import tomllib
from pathlib import Path
from typing import Annotated, Final

import msgspec
import yaml

ROOT: Final = Path(__file__).resolve().parent.parent
PLUGIN_NAME: Final = "pinboard"
EXPECTED_SKILLS: Final = frozenset({"pinboard", "pinboard-deliver", "pinboard-intake", "slop-cleanup"})
EXPECTED_ENTRY_POINTS: Final = {
    "pinboard": "pinboard.interfaces.cli:main",
}

type SkillName = Annotated[
    str,
    msgspec.Meta(max_length=64, pattern=r"\A[a-z0-9]+(?:-[a-z0-9]+)*\z"),
]
type SkillDescription = Annotated[
    str,
    msgspec.Meta(max_length=1024, pattern=r"\A[^<>]*[^<>\s][^<>]*\z"),
]
type NonBlankText = Annotated[str, msgspec.Meta(pattern=r"\A[\s\S]*\S[\s\S]*\z")]


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


class SkillFrontmatter(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    name: SkillName
    description: SkillDescription
    license: str | None = None
    allowed_tools: str | tuple[str, ...] | None = msgspec.field(name="allowed-tools", default=None)
    metadata: dict[str, str | int | float | bool | tuple[str, ...] | None] | None = None


class SkillInterface(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    display_name: NonBlankText
    short_description: NonBlankText
    default_prompt: NonBlankText


class SkillAgent(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    interface: SkillInterface


def decode_skill_frontmatter(text: str, path: Path) -> SkillFrontmatter:
    try:
        return msgspec.convert(yaml.safe_load(text), type=SkillFrontmatter, strict=True)
    except (msgspec.ValidationError, yaml.YAMLError) as error:
        raise ValueError(f"{path}: invalid metadata: {error}") from error


def decode_skill_agent(text: str, path: Path) -> SkillAgent:
    try:
        return msgspec.convert(yaml.safe_load(text), type=SkillAgent, strict=True)
    except (msgspec.ValidationError, yaml.YAMLError) as error:
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
        raise ValueError("pinboard must be the only project entry point to the current engine")


def validate_marketplace() -> None:
    path = ROOT / ".agents" / "plugins" / "marketplace.json"
    value = msgspec.json.decode(path.read_bytes(), type=MarketplaceManifest)
    if len(value.plugins) != 1:
        raise ValueError("marketplace metadata must install the repository-root plugin")
    plugin = value.plugins[0]
    if (
        value.name,
        value.interface.display_name,
        plugin.name,
        plugin.source.source,
        plugin.source.path,
        plugin.policy.installation,
        plugin.policy.authentication,
        plugin.category,
    ) != (PLUGIN_NAME, "Pinboard", PLUGIN_NAME, "local", ".", "AVAILABLE", "ON_INSTALL", "Productivity"):
        raise ValueError("marketplace metadata must install the repository-root plugin")


def validate_skill(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) < 5 or lines[0] != "---" or "---" not in lines[1:]:
        raise ValueError(f"{path}: skill frontmatter is missing")
    end = lines.index("---", 1)
    frontmatter = decode_skill_frontmatter("\n".join(lines[1:end]), path)
    if frontmatter.name != path.parent.name:
        raise ValueError(f"{path}: skill name must match its directory")
    agent = path.parent / "agents" / "openai.yaml"
    decode_skill_agent(agent.read_text(encoding="utf-8"), agent)


def main() -> None:
    validate_plugin()
    validate_project_metadata()
    validate_marketplace()
    skill_paths = tuple(sorted((ROOT / "skills").glob("*/SKILL.md")))
    if {path.parent.name for path in skill_paths} != EXPECTED_SKILLS:
        raise ValueError("public skills must be exactly pinboard, pinboard-deliver, pinboard-intake, and slop-cleanup")
    for path in skill_paths:
        validate_skill(path)
    print(f"validated plugin marketplace and {len(skill_paths)} skills")


if __name__ == "__main__":
    main()
