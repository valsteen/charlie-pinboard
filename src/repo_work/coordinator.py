from dataclasses import asdict, dataclass
from pathlib import Path

from repo_work.json_values import JsonObjectError, read_json_object, render_json_object
from repo_work.model import SCHEMA_V1


class CoordinatorError(ValueError):
    code: str

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class CoordinatorRegistration:
    schema: str
    project_root: str
    task_id: str
    host_id: str
    generation: int
    registered_at: str

    def render(self) -> bytes:
        return render_json_object(asdict(self))


def _required_string(value: dict[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise CoordinatorError("COORDINATOR_FIELD_INVALID", f"'{field}' must be a non-empty string.")
    return result


def parse_coordinator(value: dict[str, object]) -> CoordinatorRegistration:
    schema = _required_string(value, "schema")
    if schema != SCHEMA_V1:
        raise CoordinatorError("COORDINATOR_SCHEMA_INVALID", f"Coordinator must use '{SCHEMA_V1}'.")
    generation = value.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise CoordinatorError("COORDINATOR_GENERATION_INVALID", "generation must be a positive integer.")
    return CoordinatorRegistration(
        schema=schema,
        project_root=_required_string(value, "project_root"),
        task_id=_required_string(value, "task_id"),
        host_id=_required_string(value, "host_id"),
        generation=generation,
        registered_at=_required_string(value, "registered_at"),
    )


def read_coordinator(path: Path) -> CoordinatorRegistration:
    try:
        return parse_coordinator(read_json_object(path, code="COORDINATOR_INVALID", subject="coordinator registration"))
    except JsonObjectError as error:
        raise CoordinatorError(error.code, str(error).partition(": ")[2]) from error
