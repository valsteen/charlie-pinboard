#!/usr/bin/env python3

import ast
from pathlib import Path

SOURCE_ROOTS = (Path("src"), Path("tests"), Path("scripts"))


def main() -> int:
    violations: list[str] = []
    for root in SOURCE_ROOTS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            violations.extend(
                f"{path}:{node.lineno}:{node.col_offset + 1}: use a concrete type instead of object"
                for node in ast.walk(tree)
                if isinstance(node, ast.Name) and node.id == "object"
            )
    if violations:
        print("\n".join(violations))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
