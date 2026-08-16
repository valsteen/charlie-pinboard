from pathlib import Path

import msgspec

from repo_work.model import SCHEMA_V1
from repo_work.records import JsonRecord


class CoordinatorError(RuntimeError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class CoordinatorRegistration(JsonRecord):
    schema: str
    project_root: str
    task_id: str
    host_id: str
    generation: int
    registered_at: str

    def __post_init__(self) -> None:
        if self.schema != SCHEMA_V1:
            raise CoordinatorError("COORDINATOR_SCHEMA_INVALID", f"Coordinator must use '{SCHEMA_V1}'.")
        if self.generation < 1:
            raise CoordinatorError("COORDINATOR_GENERATION_INVALID", "generation must be a positive integer.")
        fields = (
            ("project_root", self.project_root),
            ("task_id", self.task_id),
            ("host_id", self.host_id),
            ("registered_at", self.registered_at),
        )
        for name, value in fields:
            if not value:
                raise CoordinatorError("COORDINATOR_FIELD_INVALID", f"'{name}' must be a non-empty string.")

    def render(self) -> bytes:
        encoded = msgspec.json.encode(self, order="sorted")
        return msgspec.json.format(encoded, indent=2) + b"\n"


def _coordinator_validation_error(error: msgspec.ValidationError) -> CoordinatorError:
    message = str(error)
    if message.startswith("Expected `object`"):
        return CoordinatorError("COORDINATOR_INVALID", "JSON root must be an object.")
    if "`generation`" in message or "$.generation" in message:
        return CoordinatorError("COORDINATOR_GENERATION_INVALID", "generation must be a positive integer.")
    return CoordinatorError("COORDINATOR_FIELD_INVALID", message)


def parse_coordinator(data: bytes | str) -> CoordinatorRegistration:
    try:
        return msgspec.json.decode(data, type=CoordinatorRegistration, strict=True)
    except msgspec.ValidationError as error:
        raise _coordinator_validation_error(error) from error
    except msgspec.DecodeError as error:
        raise CoordinatorError("COORDINATOR_INVALID", f"Cannot parse JSON: {error}") from error


def read_coordinator(path: Path) -> CoordinatorRegistration:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise CoordinatorError("COORDINATOR_INVALID", f"Cannot read JSON at '{path}': {error}") from error
    return parse_coordinator(data)
