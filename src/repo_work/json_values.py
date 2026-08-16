import json
from collections.abc import Mapping
from pathlib import Path
from typing import cast


class JsonObjectError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


def parse_json_object(text: str, *, code: str, subject: str) -> dict[str, object]:
    try:
        value: object = json.loads(text)
    except json.JSONDecodeError as error:
        raise JsonObjectError(code, f"Cannot parse {subject}: {error}") from error
    if not isinstance(value, dict):
        raise JsonObjectError(code, f"{subject} root must be an object.")
    return dict(cast(dict[str, object], value))


def read_json_object(path: Path, *, code: str, subject: str) -> dict[str, object]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as error:
        raise JsonObjectError(code, f"Cannot read {subject} at '{path}': {error}") from error
    return parse_json_object(text, code=code, subject=subject)


def render_json_object(value: Mapping[str, object]) -> bytes:
    return (json.dumps(dict(value), indent=2, sort_keys=True) + "\n").encode()
