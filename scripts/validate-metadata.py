import json
import re
from pathlib import Path
from typing import Final

ROOT: Final = Path(__file__).resolve().parent.parent
SKILL_NAME: Final = re.compile(r"^name: ([a-z0-9]+(?:-[a-z0-9]+)*)$")


def validate_plugin() -> None:
    path = ROOT / ".codex-plugin" / "plugin.json"
    value: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("plugin manifest root must be an object")
    required = {"name", "version", "description", "author", "skills", "interface"}
    missing = required - value.keys()
    if missing:
        raise ValueError(f"plugin manifest is missing: {', '.join(sorted(missing))}")
    if value.get("name") != "codex-repo-work" or value.get("skills") != "./skills/":
        raise ValueError("plugin manifest identity or skill root is invalid")


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
    skill_paths = tuple(sorted((ROOT / "skills").glob("*/SKILL.md")))
    if not skill_paths:
        raise ValueError("plugin has no skills")
    for path in skill_paths:
        validate_skill(path)
    print(f"validated plugin and {len(skill_paths)} skills")


if __name__ == "__main__":
    main()
