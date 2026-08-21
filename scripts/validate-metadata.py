import re
import tomllib
from pathlib import Path
from typing import Final, cast

import msgspec
import yaml

ROOT: Final = Path(__file__).resolve().parent.parent
SKILL_NAME: Final = re.compile(r"^name: ([a-z0-9]+(?:-[a-z0-9]+)*)$")
PLUGIN_NAME: Final = "charlie-board"
EXPECTED_SKILLS: Final = frozenset({"coordinate", "deliver", "intake"})
EXPECTED_ENTRY_POINTS: Final = {"charlie": "repo_work.cli:main", "repo-work": "repo_work.cli:main"}

type YamlScalar = str | int | float | bool | None
type YamlValue = YamlScalar | list[YamlValue] | dict[str, YamlValue]


class PluginAuthor(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    url: str


class PluginInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: str = msgspec.field(name="displayName")
    short_description: str = msgspec.field(name="shortDescription")
    long_description: str = msgspec.field(name="longDescription")
    developer_name: str = msgspec.field(name="developerName")
    category: str
    website_url: str = msgspec.field(name="websiteURL")
    capabilities: tuple[str, ...]
    default_prompt: tuple[str, ...] = msgspec.field(name="defaultPrompt")


class PluginManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    version: str
    description: str
    author: PluginAuthor
    homepage: str
    repository: str
    license: str
    keywords: tuple[str, ...]
    skills: str
    interface: PluginInterface


class ProjectMetadata(msgspec.Struct, frozen=True):
    name: str
    scripts: dict[str, str]


class MarketplaceInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: str = msgspec.field(name="displayName")


class PluginSource(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    source: str
    path: str


class PluginPolicy(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    installation: str
    authentication: str


class MarketplacePlugin(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    source: PluginSource
    policy: PluginPolicy
    category: str


class MarketplaceManifest(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: str
    interface: MarketplaceInterface
    plugins: tuple[MarketplacePlugin, ...]


class SkillFrontmatter(msgspec.Struct, frozen=True):
    name: str
    description: str


class SkillInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: str
    short_description: str
    default_prompt: str


class SkillAgent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    interface: SkillInterface


def load_yaml(text: str, path: Path) -> YamlValue:
    try:
        return cast(YamlValue, yaml.safe_load(text))
    except yaml.YAMLError as error:
        raise ValueError(f"{path}: invalid YAML: {error}") from error


def decode_skill_frontmatter(text: str, path: Path) -> SkillFrontmatter:
    value = load_yaml(text, path)
    if not isinstance(value, dict):
        raise ValueError(f"{path}: frontmatter must be a YAML mapping")
    if not all(isinstance(field, str) for field in value):
        raise ValueError(f"{path}: frontmatter field names must be strings")
    allowed = {"name", "description", "license", "allowed-tools", "metadata"}
    unexpected = set(value) - allowed
    if unexpected:
        raise ValueError(f"{path}: unexpected frontmatter fields: {', '.join(sorted(unexpected))}")
    try:
        return msgspec.convert(value, type=SkillFrontmatter, strict=True)
    except msgspec.ValidationError as error:
        raise ValueError(f"{path}: invalid metadata: {error}") from error


def decode_skill_agent(text: str, path: Path) -> SkillAgent:
    value = load_yaml(text, path)
    try:
        return msgspec.convert(value, type=SkillAgent, strict=True)
    except msgspec.ValidationError as error:
        raise ValueError(f"{path}: invalid metadata: {error}") from error


def validate_plugin() -> None:
    path = ROOT / ".codex-plugin" / "plugin.json"
    value = msgspec.json.decode(path.read_bytes(), type=PluginManifest)
    if value.name != PLUGIN_NAME or value.skills != "./skills/":
        raise ValueError("plugin manifest identity or skill root is invalid")
    if value.license != "MIT":
        raise ValueError("plugin manifest license must match the repository license")


def validate_project_metadata() -> None:
    path = ROOT / "pyproject.toml"
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    try:
        project = msgspec.convert(value["project"], type=ProjectMetadata, strict=True)
    except (KeyError, msgspec.ValidationError) as error:
        raise ValueError(f"{path}: invalid project metadata: {error}") from error
    if project.name != PLUGIN_NAME:
        raise ValueError("distribution and plugin identities must match")
    if project.scripts != EXPECTED_ENTRY_POINTS:
        raise ValueError("charlie must be primary and repo-work must remain an alias to the same engine")


def validate_marketplace() -> None:
    path = ROOT / ".agents" / "plugins" / "marketplace.json"
    value = msgspec.json.decode(path.read_bytes(), type=MarketplaceManifest)
    expected = MarketplaceManifest(
        name=PLUGIN_NAME,
        interface=MarketplaceInterface(display_name="Charlie"),
        plugins=(
            MarketplacePlugin(
                name=PLUGIN_NAME,
                source=PluginSource(source="local", path="."),
                policy=PluginPolicy(installation="AVAILABLE", authentication="ON_INSTALL"),
                category="Productivity",
            ),
        ),
    )
    if value != expected:
        raise ValueError("marketplace metadata must install the repository-root plugin")


def validate_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 5 or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{path}: skill frontmatter is missing")
    end = lines.index("---", 1)
    frontmatter = decode_skill_frontmatter("\n".join(lines[1:end]), path)
    match = SKILL_NAME.fullmatch(f"name: {frontmatter.name}")
    if match is None or match.group(1) != path.parent.name or len(frontmatter.name) > 64:
        raise ValueError(f"{path}: skill name must match its directory")
    if not frontmatter.description.strip():
        raise ValueError(f"{path}: skill description is empty")
    if "<" in frontmatter.description or ">" in frontmatter.description or len(frontmatter.description) > 1024:
        raise ValueError(f"{path}: skill description violates platform constraints")
    agent = path.parent / "agents" / "openai.yaml"
    agent_value = decode_skill_agent(agent.read_text(encoding="utf-8"), agent)
    if not all(
        value.strip()
        for value in (
            agent_value.interface.display_name,
            agent_value.interface.short_description,
            agent_value.interface.default_prompt,
        )
    ):
        raise ValueError(f"{agent}: interface fields must be non-empty")


def main() -> None:
    validate_plugin()
    validate_project_metadata()
    validate_marketplace()
    skill_paths = tuple(sorted((ROOT / "skills").glob("*/SKILL.md")))
    if {path.parent.name for path in skill_paths} != EXPECTED_SKILLS:
        raise ValueError("public skills must be exactly coordinate, deliver, and intake")
    for path in skill_paths:
        validate_skill(path)
    print(f"validated plugin marketplace and {len(skill_paths)} skills")


if __name__ == "__main__":
    main()
