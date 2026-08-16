from pathlib import Path

from attrs import frozen
from cattrs.errors import BaseValidationError

from repo_work.json_codec import (
    JsonCodecError,
    decode_json,
    encode_json,
    nested_exception,
    read_json,
    validation_message,
    validation_paths,
)
from repo_work.model import SCHEMA_V1


class CoordinatorError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@frozen
class CoordinatorRegistration:
    schema: str
    project_root: str
    task_id: str
    host_id: str
    generation: int
    registered_at: str

    def __attrs_post_init__(self) -> None:
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
        return encode_json(self)


def _coordinator_validation_error(error: BaseValidationError) -> CoordinatorError:
    domain_error = nested_exception(error, CoordinatorError)
    if domain_error is not None:
        return domain_error
    if any(path and path[0] == "generation" for path in validation_paths(error)):
        return CoordinatorError("COORDINATOR_GENERATION_INVALID", "generation must be a positive integer.")
    return CoordinatorError("COORDINATOR_FIELD_INVALID", validation_message(error))


def parse_coordinator(data: bytes | str) -> CoordinatorRegistration:
    try:
        return decode_json(data, CoordinatorRegistration)
    except JsonCodecError as error:
        raise CoordinatorError("COORDINATOR_INVALID", error.message) from error
    except BaseValidationError as error:
        raise _coordinator_validation_error(error) from error


def read_coordinator(path: Path) -> CoordinatorRegistration:
    try:
        return read_json(path, CoordinatorRegistration)
    except JsonCodecError as error:
        raise CoordinatorError("COORDINATOR_INVALID", error.message) from error
    except BaseValidationError as error:
        raise _coordinator_validation_error(error) from error
