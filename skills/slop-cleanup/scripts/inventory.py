#!/usr/bin/env python3
"""Emit a deterministic cleanup inventory without claiming semantic reachability."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath

IDENTIFIER = r"[A-Za-z_][A-Za-z0-9_]*"
LANGUAGE_BY_SUFFIX = {
    ".cjs": "typescript",
    ".go": "go",
    ".js": "typescript",
    ".jsx": "typescript",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".mjs": "typescript",
    ".py": "python",
    ".rs": "rust",
    ".sql": "sql",
    ".ts": "typescript",
    ".tsx": "typescript",
}
ASSET_SUFFIXES = frozenset({".gif", ".ico", ".jpeg", ".jpg", ".png", ".svg", ".webp"})
DECLARATION_PATTERNS = {
    "go": (
        (
            "declaration",
            re.compile(rf"^\s*(?:type|func|var|const)\s+(?:\([^\n]*\)\s+)?({IDENTIFIER})\b", re.MULTILINE),
        ),
    ),
    "python": (
        ("class", re.compile(rf"^[ \t]*class\s+({IDENTIFIER})\b", re.MULTILINE)),
        ("function", re.compile(rf"^[ \t]*(?:async\s+)?def\s+({IDENTIFIER})\b", re.MULTILINE)),
        ("type", re.compile(rf"^[ \t]*type\s+({IDENTIFIER})\b", re.MULTILINE)),
        ("constant", re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?::[^=\n]+)?=", re.MULTILINE)),
    ),
    "rust": (
        (
            "declaration",
            re.compile(
                rf"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|type|const|static|mod)\s+({IDENTIFIER})\b",
                re.MULTILINE,
            ),
        ),
    ),
    "kotlin": (
        (
            "declaration",
            re.compile(
                rf"^\s*(?:(?:public|private|internal|protected|data|sealed|enum|value|annotation|suspend|inline|open|abstract)\s+)*(?:class|interface|object|fun|typealias|val|var)\s+({IDENTIFIER})\b",
                re.MULTILINE,
            ),
        ),
    ),
    "typescript": (
        (
            "declaration",
            re.compile(
                rf"^\s*(?:(?:export|default|declare|async)\s+)*(?:function|class|interface|type|enum|const|let|var|namespace)\s+({IDENTIFIER})\b",
                re.MULTILINE,
            ),
        ),
    ),
}
BRACE_ENUM = re.compile(rf"\b(?:enum\s+class|enum)\s+({IDENTIFIER})[^{{]*{{")
PYTHON_ENUM = re.compile(rf"^(?P<indent>[ \t]*)class\s+(?P<name>{IDENTIFIER})\s*\([^)]*Enum[^)]*\)\s*:", re.MULTILINE)
SQL_OBJECT = re.compile(
    rf"\bCREATE\s+(?:UNIQUE\s+)?(TABLE|INDEX|VIEW|TRIGGER)\s+(?:IF\s+NOT\s+EXISTS\s+)?[\"`\[]?({IDENTIFIER})",
    re.IGNORECASE,
)
SQL_CHECK_IN = re.compile(
    rf"CHECK\s*\(\s*({IDENTIFIER})\s+IN\s*\(([^)]*)\)\s*\)",
    re.IGNORECASE | re.DOTALL,
)
QUOTED_VALUE = re.compile(r"'([^']+)'|\"([^\"]+)\"")
IDENTIFIER_TOKEN = re.compile(IDENTIFIER)
MEMBER_TOKEN = re.compile(rf"\.({IDENTIFIER})\b")


@dataclass(frozen=True)
class SourceFile:
    path: str
    role: str
    language: str
    text: str | None
    content_sha256: str


@dataclass(frozen=True)
class Declaration:
    selector: str
    path: str
    line: int
    language: str
    kind: str
    name: str
    source: str
    production_uses: int = 0
    test_uses: int = 0
    support_uses: int = 0
    ambiguous_name: bool = False


@dataclass(frozen=True)
class AtomUse:
    atom: str
    production_uses: int
    production_declaration_uses: int
    production_non_declaration_uses: int
    test_uses: int
    support_uses: int


@dataclass(frozen=True)
class ClosedFamily:
    selector: str
    path: str
    line: int
    language: str
    kind: str
    name: str
    atoms: tuple[str, ...]
    atom_uses: tuple[AtomUse, ...] = ()


@dataclass(frozen=True)
class SchemaObject:
    selector: str
    path: str
    line: int
    kind: str
    name: str


@dataclass(frozen=True)
class Coverage:
    category: str
    status: str
    method: str


@dataclass(frozen=True)
class AmbiguousDefinition:
    selector: str
    line: int
    kind: str


@dataclass(frozen=True)
class AmbiguousDefinitionGroup:
    name: str
    production_uses: int
    test_uses: int
    support_uses: int
    definitions: tuple[AmbiguousDefinition, ...]


@dataclass(frozen=True)
class ReferenceCounts:
    production: Counter[str]
    test: Counter[str]
    support: Counter[str]
    production_members: Counter[str]
    test_members: Counter[str]
    support_members: Counter[str]
    production_literals: Counter[str]
    test_literals: Counter[str]
    support_literals: Counter[str]
    production_texts: tuple[str, ...]
    test_texts: tuple[str, ...]
    support_texts: tuple[str, ...]

    def count(self, name: str, role: str) -> int:
        counter, texts = self.values(role)
        if IDENTIFIER_TOKEN.fullmatch(name):
            return counter[name]
        return sum(text.count(name) for text in texts)

    def values(self, role: str) -> tuple[Counter[str], tuple[str, ...]]:
        if role == "production":
            return self.production, self.production_texts
        if role == "test":
            return self.test, self.test_texts
        if role == "support":
            return self.support, self.support_texts
        raise ValueError(f"unknown source role: {role}")

    def member_count(self, name: str, role: str) -> int:
        if role == "production":
            return self.production_members[name]
        if role == "test":
            return self.test_members[name]
        if role == "support":
            return self.support_members[name]
        raise ValueError(f"unknown source role: {role}")

    def literal_count(self, value: str, role: str) -> int:
        if role == "production":
            return self.production_literals[value]
        if role == "test":
            return self.test_literals[value]
        if role == "support":
            return self.support_literals[value]
        raise ValueError(f"unknown source role: {role}")


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    parser.add_argument("--production-root", action="append", required=True)
    parser.add_argument("--test-root", action="append", default=[])
    parser.add_argument("--mode", choices=("generic", "python-ast"), required=True)
    return parser.parse_args()


def repository_paths(repository: Path) -> tuple[str, ...]:
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        check=True,
        capture_output=True,
    )
    paths = {path.decode("utf-8") for path in result.stdout.split(b"\0") if path}
    return tuple(sorted(path for path in paths if (repository / path).is_file()))


def normalized_roots(values: list[str]) -> tuple[PurePosixPath, ...]:
    roots = tuple(PurePosixPath(value) for value in values)
    if any(root.is_absolute() or ".." in root.parts for root in roots):
        raise ValueError("inventory roots must be repository-relative paths without '..'")
    return roots


def require_existing_roots(repository: Path, roots: tuple[PurePosixPath, ...]) -> None:
    for root in roots:
        if not (repository / root).exists():
            raise SystemExit(f"inventory root does not exist: {root}")


def within(path: PurePosixPath, roots: tuple[PurePosixPath, ...]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def read_sources(
    repository: Path,
    production_roots: tuple[PurePosixPath, ...],
    test_roots: tuple[PurePosixPath, ...],
) -> tuple[SourceFile, ...]:
    sources = []
    for relative_path in repository_paths(repository):
        posix_path = PurePosixPath(relative_path)
        role = (
            "production"
            if within(posix_path, production_roots)
            else "test"
            if within(posix_path, test_roots)
            else "support"
        )
        content = (repository / relative_path).read_bytes()
        try:
            text = None if b"\0" in content else content.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        sources.append(
            SourceFile(
                relative_path,
                role,
                LANGUAGE_BY_SUFFIX.get(posix_path.suffix.lower(), "other"),
                text,
                hashlib.sha256(content).hexdigest(),
            )
        )
    return tuple(sources)


def line_number(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def generic_declarations(source: SourceFile) -> list[Declaration]:
    if source.text is None:
        return []
    declarations = []
    for kind, pattern in DECLARATION_PATTERNS.get(source.language, ()):
        for match in pattern.finditer(source.text):
            name = match.group(1)
            declaration_kind = "method" if kind == "function" and match.group(0)[0].isspace() else kind
            declarations.append(
                Declaration(
                    selector=f"{source.path}::{name}",
                    path=source.path,
                    line=line_number(source.text, match.start(1)),
                    language=source.language,
                    kind=declaration_kind,
                    name=name,
                    source="generic",
                )
            )
    return declarations


def python_declaration(
    node: ast.stmt, source: SourceFile, owner: str | None, include_constants: bool
) -> Declaration | None:
    name = None
    kind = None
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        name, kind = node.name, "method" if owner else "function"
    elif isinstance(node, ast.ClassDef):
        name, kind = node.name, "class"
    elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
        name, kind = node.name.id, "type"
    elif include_constants and isinstance(node, (ast.Assign, ast.AnnAssign)):
        target = (
            node.targets[0]
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            else node.target
            if isinstance(node, ast.AnnAssign)
            else None
        )
        if isinstance(target, ast.Name) and target.id.isupper():
            name, kind = target.id, "constant"
    if name is None or kind is None:
        return None
    qualified_name = f"{owner}.{name}" if owner else name
    return Declaration(
        selector=f"{source.path}::{qualified_name}",
        path=source.path,
        line=node.lineno,
        language="python",
        kind=kind,
        name=name,
        source="python-ast",
    )


def is_enum_class(node: ast.ClassDef) -> bool:
    return any(
        (isinstance(base, ast.Name) and base.id == "Enum") or (isinstance(base, ast.Attribute) and base.attr == "Enum")
        for base in node.bases
    )


def python_enum_family(node: ast.ClassDef, source: SourceFile, qualified_name: str) -> ClosedFamily | None:
    if not is_enum_class(node):
        return None
    atoms = tuple(
        target.id
        for member in node.body
        if isinstance(member, (ast.Assign, ast.AnnAssign))
        for target in (
            [member.targets[0]]
            if isinstance(member, ast.Assign) and len(member.targets) == 1
            else [member.target]
            if isinstance(member, ast.AnnAssign)
            else []
        )
        if isinstance(target, ast.Name) and not target.id.startswith("_")
    )
    if not atoms:
        return None
    return ClosedFamily(
        selector=f"{source.path}::{qualified_name}",
        path=source.path,
        line=node.lineno,
        language="python",
        kind="enum",
        name=node.name,
        atoms=atoms,
    )


def python_enum_value_family(node: ast.ClassDef, source: SourceFile, qualified_name: str) -> ClosedFamily | None:
    if not is_enum_class(node):
        return None
    atoms = tuple(
        str(value.value)
        for member in node.body
        for value in (
            [member.value]
            if isinstance(member, ast.Assign) and len(member.targets) == 1
            else [member.value]
            if isinstance(member, ast.AnnAssign) and isinstance(member.target, ast.Name)
            else []
        )
        if isinstance(value, ast.Constant) and isinstance(value.value, (str, int))
    )
    if len(atoms) <= 1:
        return None
    return ClosedFamily(
        selector=f"{source.path}::{qualified_name}.value",
        path=source.path,
        line=node.lineno,
        language="python",
        kind="enum-values",
        name=f"{node.name}.value",
        atoms=atoms,
    )


def python_union_family(node: ast.TypeAlias, source: SourceFile) -> ClosedFamily | None:
    if not isinstance(node.name, ast.Name):
        return None
    atoms = union_atoms(node.value)
    if len(atoms) <= 1:
        return None
    return ClosedFamily(
        selector=f"{source.path}::{node.name.id}",
        path=source.path,
        line=node.lineno,
        language="python",
        kind="union",
        name=node.name.id,
        atoms=atoms,
    )


def literal_atoms(node: ast.expr) -> tuple[str, ...]:
    if not (
        isinstance(node, ast.Subscript)
        and (
            (isinstance(node.value, ast.Name) and node.value.id == "Literal")
            or (isinstance(node.value, ast.Attribute) and node.value.attr == "Literal")
        )
    ):
        return ()
    values = node.slice.elts if isinstance(node.slice, ast.Tuple) else (node.slice,)
    return tuple(str(value.value) if isinstance(value, ast.Constant) else ast.unparse(value) for value in values)


def python_literal_family(
    annotation: ast.expr, source: SourceFile, qualified_name: str, line: int
) -> ClosedFamily | None:
    atoms = literal_atoms(annotation)
    if len(atoms) <= 1:
        return None
    return ClosedFamily(
        selector=f"{source.path}::{qualified_name}",
        path=source.path,
        line=line,
        language="python",
        kind="literal",
        name=qualified_name,
        atoms=atoms,
    )


def msgspec_tag(node: ast.ClassDef) -> tuple[str, str] | None:
    is_struct = any(
        (isinstance(base, ast.Name) and base.id == "Struct")
        or (isinstance(base, ast.Attribute) and base.attr == "Struct")
        for base in node.bases
    )
    if not is_struct:
        return None
    keywords = {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg is not None}
    tag = keywords.get("tag")
    tag_field = keywords.get("tag_field")
    if not isinstance(tag, ast.Constant) or not isinstance(tag.value, str):
        return None
    return (
        tag.value,
        tag_field.value if isinstance(tag_field, ast.Constant) and isinstance(tag_field.value, str) else "type",
    )


def python_type_alias_family(node: ast.TypeAlias, source: SourceFile) -> ClosedFamily | None:
    return python_literal_family(node.value, source, node.name.id, node.lineno) or python_union_family(node, source)


def visit_python_body(
    body: list[ast.stmt],
    source: SourceFile,
    declarations: list[Declaration],
    families: list[ClosedFamily],
    tagged_structs: dict[str, tuple[str, str]],
    owner: str | None = None,
    *,
    include_constants: bool = True,
) -> None:
    for node in body:
        if declaration := python_declaration(node, source, owner, include_constants):
            declarations.append(declaration)
        if isinstance(node, ast.ClassDef):
            qualified_name = f"{owner}.{node.name}" if owner else node.name
            if tagged := msgspec_tag(node):
                tagged_structs[qualified_name] = tagged
            if family := python_enum_family(node, source, qualified_name):
                families.append(family)
            if family := python_enum_value_family(node, source, qualified_name):
                families.append(family)
            visit_python_body(
                node.body,
                source,
                declarations,
                families,
                tagged_structs,
                qualified_name,
                include_constants=not is_enum_class(node),
            )
        elif isinstance(node, ast.TypeAlias) and (family := python_type_alias_family(node, source)):
            families.append(family)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            qualified_name = f"{owner}.{node.target.id}" if owner else node.target.id
            if family := python_literal_family(node.annotation, source, qualified_name, node.lineno):
                families.append(family)


def python_tagged_union_family(family: ClosedFamily, tagged_structs: dict[str, tuple[str, str]]) -> ClosedFamily | None:
    if family.kind != "union" or not all(atom in tagged_structs for atom in family.atoms):
        return None
    tags = tuple(tagged_structs[atom] for atom in family.atoms)
    tag_fields = {tag_field for _, tag_field in tags}
    if len(tag_fields) != 1:
        return None
    tag_field = next(iter(tag_fields))
    return ClosedFamily(
        selector=f"{family.selector}.{tag_field}",
        path=family.path,
        line=family.line,
        language="python",
        kind="msgspec-tagged-union",
        name=f"{family.name}.{tag_field}",
        atoms=tuple(tag for tag, _ in tags),
    )


def python_ast_inventory(source: SourceFile) -> tuple[list[Declaration], list[ClosedFamily]]:
    if source.text is None:
        return [], []
    tree = ast.parse(source.text, filename=source.path)
    declarations: list[Declaration] = []
    families: list[ClosedFamily] = []
    tagged_structs: dict[str, tuple[str, str]] = {}
    visit_python_body(tree.body, source, declarations, families, tagged_structs)
    families.extend(
        tagged_family
        for family in tuple(families)
        if (tagged_family := python_tagged_union_family(family, tagged_structs)) is not None
    )
    return declarations, families


def union_atoms(node: ast.expr) -> tuple[str, ...]:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return (*union_atoms(node.left), *union_atoms(node.right))
    return (ast.unparse(node),)


def matching_brace(text: str, opening: int) -> int | None:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return None


def generic_closed_families(source: SourceFile) -> list[ClosedFamily]:
    if source.text is None:
        return []
    if source.language == "python":
        return generic_python_enums(source)
    if source.language not in {"kotlin", "rust", "typescript"}:
        return []
    families = []
    for match in BRACE_ENUM.finditer(source.text):
        closing = matching_brace(source.text, match.end() - 1)
        if closing is None:
            continue
        body = source.text[match.end() : closing]
        atoms = []
        depth = 1
        for line in body.splitlines():
            if depth == 1:
                atom_match = re.match(rf"\s*({IDENTIFIER})\s*(?:[,=({{]|$)", line)
                if atom_match and atom_match.group(1) not in {"fun", "val", "var"}:
                    atoms.append(atom_match.group(1))
            depth += line.count("{") - line.count("}")
        if atoms:
            name = match.group(1)
            families.append(
                ClosedFamily(
                    selector=f"{source.path}::{name}",
                    path=source.path,
                    line=line_number(source.text, match.start(1)),
                    language=source.language,
                    kind="enum",
                    name=name,
                    atoms=tuple(atoms),
                )
            )
    return families


def generic_python_enums(source: SourceFile) -> list[ClosedFamily]:
    assert source.text is not None
    lines = source.text.splitlines()
    families: list[ClosedFamily] = []
    for match in PYTHON_ENUM.finditer(source.text):
        indent = len(match.group("indent"))
        atoms = []
        for line in lines[line_number(source.text, match.start()) :]:
            if not line.strip():
                continue
            member_indent = len(line) - len(line.lstrip())
            if member_indent <= indent:
                break
            member = re.match(rf"\s*({IDENTIFIER})\s*=", line)
            if member and not member.group(1).startswith("_"):
                atoms.append(member.group(1))
        if atoms:
            name = match.group("name")
            families.append(
                ClosedFamily(
                    selector=f"{source.path}::{name}",
                    path=source.path,
                    line=line_number(source.text, match.start("name")),
                    language="python",
                    kind="enum",
                    name=name,
                    atoms=tuple(atoms),
                )
            )
    return families


def sql_inventory(sources: tuple[SourceFile, ...]) -> tuple[list[SchemaObject], list[ClosedFamily]]:
    schema_objects = []
    families = []
    for source in sources:
        if source.role != "production" or source.text is None:
            continue
        table_positions = [
            (match.start(), match.group(2))
            for match in SQL_OBJECT.finditer(source.text)
            if match.group(1).lower() == "table"
        ]
        for match in SQL_OBJECT.finditer(source.text):
            kind, name = match.group(1).lower(), match.group(2)
            line = line_number(source.text, match.start(2))
            schema_objects.append(SchemaObject(f"{source.path}::{kind}:{name}", source.path, line, kind, name))
        for match in SQL_CHECK_IN.finditer(source.text):
            values = tuple(first or second for first, second in QUOTED_VALUE.findall(match.group(2)))
            if not values:
                continue
            table = next(
                (name for offset, name in reversed(table_positions) if offset < match.start()), "<unknown-table>"
            )
            column = match.group(1)
            line = line_number(source.text, match.start(1))
            families.append(
                ClosedFamily(
                    selector=f"{source.path}::{table}.{column}@{line}",
                    path=source.path,
                    line=line,
                    language="sql",
                    kind="check-in",
                    name=f"{table}.{column}",
                    atoms=values,
                )
            )
    return schema_objects, families


def reference_counts(sources: tuple[SourceFile, ...]) -> ReferenceCounts:
    texts_by_role = {
        role: tuple(source.text for source in sources if source.role == role and source.text is not None)
        for role in ("production", "test", "support")
    }
    counters = {
        role: Counter(token for text in texts for token in IDENTIFIER_TOKEN.findall(text))
        for role, texts in texts_by_role.items()
    }
    member_counters = {
        role: Counter(member for text in texts for member in MEMBER_TOKEN.findall(text))
        for role, texts in texts_by_role.items()
    }
    literal_counters = {
        role: Counter(first or second for text in texts for first, second in QUOTED_VALUE.findall(text))
        for role, texts in texts_by_role.items()
    }
    return ReferenceCounts(
        production=counters["production"],
        test=counters["test"],
        support=counters["support"],
        production_members=member_counters["production"],
        test_members=member_counters["test"],
        support_members=member_counters["support"],
        production_literals=literal_counters["production"],
        test_literals=literal_counters["test"],
        support_literals=literal_counters["support"],
        production_texts=texts_by_role["production"],
        test_texts=texts_by_role["test"],
        support_texts=texts_by_role["support"],
    )


def add_reference_counts(
    declarations: list[Declaration],
    families: list[ClosedFamily],
    sources: tuple[SourceFile, ...],
) -> tuple[list[Declaration], list[ClosedFamily]]:
    references = reference_counts(sources)
    declaration_atom_counts = Counter(atom for family in families for atom in family.atoms)
    enum_value_members = linked_enum_value_members(families)
    declaration_counts = Counter(declaration.name for declaration in declarations if declaration.kind != "method")
    name_counts = Counter(declaration.name for declaration in declarations)
    counted_declarations = [
        Declaration(
            **{
                **asdict(declaration),
                "production_uses": reference_count(
                    references, declaration, "production", declaration_counts[declaration.name]
                ),
                "test_uses": reference_count(references, declaration, "test", 0),
                "support_uses": reference_count(references, declaration, "support", 0),
                "ambiguous_name": name_counts[declaration.name] > 1,
            }
        )
        for declaration in declarations
    ]
    counted_families = []
    for family in families:
        atom_uses = tuple(
            counted_atom_use(
                references,
                family,
                atom,
                declaration_atom_counts[atom],
                enum_value_members.get((family.selector, atom)),
            )
            for atom in family.atoms
        )
        counted_families.append(ClosedFamily(**{**asdict(family), "atom_uses": atom_uses}))
    return counted_declarations, counted_families


def linked_enum_value_members(families: list[ClosedFamily]) -> dict[tuple[str, str], str]:
    families_by_selector = {family.selector: family for family in families}
    linked: dict[tuple[str, str], str] = {}
    for family in families:
        if family.kind != "enum-values":
            continue
        member_family = families_by_selector.get(family.selector.removesuffix(".value"))
        if member_family is None or member_family.kind != "enum" or len(member_family.atoms) != len(family.atoms):
            continue
        linked.update(
            ((family.selector, value), member) for value, member in zip(family.atoms, member_family.atoms, strict=True)
        )
    return linked


def counted_atom_use(
    references: ReferenceCounts,
    family: ClosedFamily,
    atom: str,
    declared_uses: int,
    linked_member: str | None,
) -> AtomUse:
    production_occurrences = atom_reference_count(references, family, atom, "production")
    production_declaration_uses = min(production_occurrences, declared_uses)
    production_non_declaration_uses = (
        production_occurrences
        - production_declaration_uses
        + (references.member_count(linked_member, "production") if linked_member else 0)
    )
    return AtomUse(
        atom=atom,
        production_uses=production_non_declaration_uses,
        production_declaration_uses=production_declaration_uses,
        production_non_declaration_uses=production_non_declaration_uses,
        test_uses=atom_reference_count(references, family, atom, "test")
        + (references.member_count(linked_member, "test") if linked_member else 0),
        support_uses=atom_reference_count(references, family, atom, "support")
        + (references.member_count(linked_member, "support") if linked_member else 0),
    )


def atom_reference_count(references: ReferenceCounts, family: ClosedFamily, atom: str, role: str) -> int:
    if family.kind in {"check-in", "enum-values", "msgspec-tagged-union"} or (
        family.kind == "literal" and IDENTIFIER_TOKEN.fullmatch(atom)
    ):
        return references.literal_count(atom, role)
    return references.count(atom, role)


def reference_count(references: ReferenceCounts, declaration: Declaration, role: str, declarations: int) -> int:
    if declaration.kind == "method":
        return references.member_count(declaration.name, role)
    return max(0, references.count(declaration.name, role) - declarations)


def ambiguous_zero_production_definitions(
    declarations: list[Declaration],
) -> list[AmbiguousDefinitionGroup]:
    grouped: dict[str, list[Declaration]] = {}
    for declaration in declarations:
        if declaration.ambiguous_name and not is_implicit_protocol(declaration):
            grouped.setdefault(declaration.name, []).append(declaration)
    return [
        AmbiguousDefinitionGroup(
            name=name,
            production_uses=0,
            test_uses=max(item.test_uses for item in items),
            support_uses=max(item.support_uses for item in items),
            definitions=tuple(
                AmbiguousDefinition(item.selector, item.line, item.kind)
                for item in sorted(items, key=declaration_position)
            ),
        )
        for name, items in sorted(grouped.items())
        if all(item.production_uses == 0 for item in items)
    ]


def is_implicit_protocol(declaration: Declaration) -> bool:
    return declaration.kind == "method" and declaration.name.startswith("__") and declaration.name.endswith("__")


def declaration_position(declaration: Declaration) -> tuple[str, int]:
    return declaration.path, declaration.line


def declaration_sort_key(declaration: Declaration) -> tuple[str, int, str]:
    return declaration.path, declaration.line, declaration.name


def family_sort_key(family: ClosedFamily) -> tuple[str, int, str]:
    return family.path, family.line, family.name


def schema_object_sort_key(schema_object: SchemaObject) -> tuple[str, int, str]:
    return schema_object.path, schema_object.line, schema_object.name


def input_digest(
    sources: tuple[SourceFile, ...], production_roots: tuple[PurePosixPath, ...], test_roots: tuple[PurePosixPath, ...]
) -> str:
    digest = hashlib.sha256()
    for root in (*production_roots, *test_roots):
        digest.update(str(root).encode())
        digest.update(b"\0")
    for source in sources:
        digest.update(source.path.encode())
        digest.update(b"\0")
        digest.update(source.content_sha256.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def unreferenced_assets(sources: tuple[SourceFile, ...]) -> list[str]:
    return [
        source.path
        for source in sources
        if PurePosixPath(source.path).suffix.lower() in ASSET_SUFFIXES
        and not any(
            other.path != source.path
            and other.text is not None
            and (source.path in other.text or PurePosixPath(source.path).name in other.text)
            for other in sources
        )
    ]


def duplicated_closed_vocabularies(families: list[ClosedFamily]) -> list[dict[str, list[str]]]:
    grouped: dict[tuple[str, ...], list[str]] = {}
    for family in families:
        normalized_atoms = tuple(sorted(atom.casefold() for atom in family.atoms))
        if len(normalized_atoms) > 1:
            grouped.setdefault(normalized_atoms, []).append(family.selector)
    return [
        {"atoms": list(atoms), "families": sorted(selectors)}
        for atoms, selectors in sorted(grouped.items())
        if len(selectors) > 1
    ]


def coverage(mode: str) -> tuple[Coverage, ...]:
    return (
        Coverage(
            "repository-files",
            "complete",
            "tracked and untracked non-ignored working-tree files from git ls-files",
        ),
        Coverage(
            "generic-declarations",
            "partial",
            "conservative line patterns for Go, Kotlin, Python, Rust, and TypeScript",
        ),
        Coverage(
            "lexical-references",
            "partial",
            "identifier and literal counts; imports, scopes, reflection, and generated use remain unresolved",
        ),
        Coverage(
            "closed-families",
            "partial",
            "enum-shaped declarations plus SQL CHECK IN vocabularies; Go constant groups require another analyzer",
        ),
        Coverage("sql-schema", "partial", "CREATE objects and literal CHECK IN vocabularies, including embedded SQL"),
        Coverage("assets", "partial", "tracked common asset paths and basenames referenced by tracked text"),
        Coverage(
            "python-ast",
            "complete" if mode == "python-ast" else "unsupported",
            "stdlib ast definitions, Enum members, Literal vocabularies, explicit union aliases, and msgspec tags"
            if mode == "python-ast"
            else "mode disabled",
        ),
        Coverage("semantic-producer-consumer", "unsupported", "requires product authority and code-path inspection"),
        Coverage(
            "dynamic-reachability", "unsupported", "requires repository-specific registration and runtime inspection"
        ),
        Coverage(
            "navigation-and-structural-smells", "unsupported", "requires representative traces and semantic comparison"
        ),
    )


def main() -> None:
    arguments = parse_arguments()
    repository = arguments.repository.resolve()
    production_roots = normalized_roots(arguments.production_root)
    test_roots = normalized_roots(arguments.test_root)
    require_existing_roots(repository, (*production_roots, *test_roots))
    sources = read_sources(repository, production_roots, test_roots)
    declarations: list[Declaration] = []
    families: list[ClosedFamily] = []
    for source in sources:
        if source.role != "production":
            continue
        if arguments.mode == "python-ast" and source.language == "python":
            python_declarations, python_families = python_ast_inventory(source)
            declarations.extend(python_declarations)
            families.extend(python_families)
        else:
            declarations.extend(generic_declarations(source))
            families.extend(generic_closed_families(source))
    schema_objects, sql_families = sql_inventory(sources)
    families.extend(sql_families)
    declarations, families = add_reference_counts(declarations, families, sources)
    declarations.sort(key=declaration_sort_key)
    families.sort(key=family_sort_key)
    schema_objects.sort(key=schema_object_sort_key)
    candidates = {
        "test_only_definitions": [
            asdict(item)
            for item in declarations
            if item.production_uses == 0
            and item.test_uses > 0
            and not item.ambiguous_name
            and not is_implicit_protocol(item)
        ],
        "unreferenced_definitions": [
            asdict(item)
            for item in declarations
            if item.production_uses == 0
            and item.test_uses == 0
            and not item.ambiguous_name
            and not is_implicit_protocol(item)
        ],
        "low_reference_closed_atoms": [
            {"family": family.selector, **asdict(atom_use)}
            for family in families
            for atom_use in family.atom_uses
            if atom_use.production_uses <= 1
        ],
        "closed_atoms_without_apparent_non_declaration_production_use": [
            {"family": family.selector, **asdict(atom_use)}
            for family in families
            for atom_use in family.atom_uses
            if atom_use.production_non_declaration_uses == 0
        ],
        "duplicated_closed_vocabularies": duplicated_closed_vocabularies(families),
        "ambiguous_zero_production_definitions": [
            asdict(item) for item in ambiguous_zero_production_definitions(declarations)
        ],
        "implicit_protocol_definitions": [asdict(item) for item in declarations if is_implicit_protocol(item)],
        "unreferenced_assets": unreferenced_assets(sources),
    }
    report = {
        "schema": "slop-cleanup-inventory/v1",
        "mode": arguments.mode,
        "repository": str(repository),
        "input_digest": input_digest(sources, production_roots, test_roots),
        "roots": {
            "production": [str(root) for root in production_roots],
            "test": [str(root) for root in test_roots],
        },
        "coverage": [asdict(item) for item in coverage(arguments.mode)],
        "summary": {
            "repository_files": len(sources),
            "text_files": sum(source.text is not None for source in sources),
            "binary_files": sum(source.text is None for source in sources),
            "languages": sorted(
                {source.language for source in sources if source.role == "production" and source.language != "other"}
            ),
            "declarations": len(declarations),
            "closed_families": len(families),
            "closed_family_atoms": sum(len(family.atoms) for family in families),
            "schema_objects": len(schema_objects),
        },
        "declarations": [asdict(item) for item in declarations],
        "closed_families": [asdict(item) for item in families],
        "schema_objects": [asdict(item) for item in schema_objects],
        "candidates": candidates,
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
