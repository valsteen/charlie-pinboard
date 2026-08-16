from pathlib import Path
from typing import Annotated

import msgspec

from repo_work.model import SchemaV1

type NonEmptyString = Annotated[str, msgspec.Meta(min_length=1)]
type PositiveGeneration = Annotated[int, msgspec.Meta(ge=1)]


class CoordinatorError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class CoordinatorRegistration(msgspec.Struct, frozen=True, forbid_unknown_fields=True):
    schema: SchemaV1
    project_root: NonEmptyString
    task_id: NonEmptyString
    host_id: NonEmptyString
    generation: PositiveGeneration
    registered_at: NonEmptyString

    def render(self) -> bytes:
        encoded = msgspec.json.encode(self, order="sorted")
        return msgspec.json.format(encoded, indent=2) + b"\n"


def parse_coordinator(data: bytes | str) -> CoordinatorRegistration:
    try:
        return msgspec.json.decode(data, type=CoordinatorRegistration)
    except msgspec.DecodeError as error:
        raise CoordinatorError("COORDINATOR_INVALID", f"Cannot decode coordinator JSON: {error}") from error


def read_coordinator(path: Path) -> CoordinatorRegistration:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise CoordinatorError("COORDINATOR_INVALID", f"Cannot read JSON at '{path}': {error}") from error
    return parse_coordinator(data)
