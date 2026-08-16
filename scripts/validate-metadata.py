import re
from pathlib import Path
from typing import Final

from attrs import frozen

from repo_work.json_codec import CONVERTER, register_json_array

ROOT: Final = Path(__file__).resolve().parent.parent
SKILL_NAME: Final = re.compile(r"^name: ([a-z0-9]+(?:-[a-z0-9]+)*)$")
PLUGIN_NAME: Final = "codex-repo-work"


@frozen
class PluginAuthor:
    name: str
    url: str


@frozen
class PluginInterface:
    displayName: str
    shortDescription: str
    longDescription: str
    developerName: str
    category: str
    websiteURL: str
    capabilities: tuple[str, ...]
    defaultPrompt: tuple[str, ...]


@frozen
class PluginManifest:
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


@frozen
class MarketplaceInterface:
    displayName: str


@frozen
class PluginSource:
    source: str
    path: str


@frozen
class PluginPolicy:
    installation: str
    authentication: str


@frozen
class MarketplacePlugin:
    name: str
    source: PluginSource
    policy: PluginPolicy
    category: str


@frozen
class MarketplaceManifest:
    name: str
    interface: MarketplaceInterface
    plugins: tuple[MarketplacePlugin, ...]


register_json_array(tuple[MarketplacePlugin, ...], MarketplacePlugin)


def validate_plugin() -> None:
    path = ROOT / ".codex-plugin" / "plugin.json"
    value = CONVERTER.loads(path.read_bytes(), PluginManifest)
    if value.name != PLUGIN_NAME or value.skills != "./skills/":
        raise ValueError("plugin manifest identity or skill root is invalid")
    if value.license != "MIT":
        raise ValueError("plugin manifest license must match the repository license")


def validate_marketplace() -> None:
    path = ROOT / ".agents" / "plugins" / "marketplace.json"
    value = CONVERTER.loads(path.read_bytes(), MarketplaceManifest)
    expected = MarketplaceManifest(
        name=PLUGIN_NAME,
        interface=MarketplaceInterface(displayName="Codex Repository Work"),
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
    name_line = next((line for line in lines[1:end] if line.startswith("name: ")), "")
    description = next((line for line in lines[1:end] if line.startswith("description: ")), "")
    match = SKILL_NAME.fullmatch(name_line)
    if match is None or match.group(1) != path.parent.name:
        raise ValueError(f"{path}: skill name must match its directory")
    if not description.removeprefix("description: ").strip():
        raise ValueError(f"{path}: skill description is empty")
    agent = path.parent / "agents" / "openai.yaml"
    agent_text = agent.read_text(encoding="utf-8")
    for field in ("display_name:", "short_description:", "default_prompt:"):
        if field not in agent_text:
            raise ValueError(f"{agent}: missing {field}")


def main() -> None:
    validate_plugin()
    validate_marketplace()
    skill_paths = tuple(sorted((ROOT / "skills").glob("*/SKILL.md")))
    if not skill_paths:
        raise ValueError("plugin has no skills")
    for path in skill_paths:
        validate_skill(path)
    print(f"validated plugin marketplace and {len(skill_paths)} skills")


if __name__ == "__main__":
    main()
