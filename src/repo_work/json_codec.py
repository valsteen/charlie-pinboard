import json
from pathlib import Path
from typing import Final

from cattrs.errors import BaseValidationError, ClassValidationError, IterableValidationError
from cattrs.preconf.json import JsonConverter, make_converter

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


class JsonCodecError(ValueError):
    message: str

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def _exact_string(value: JsonValue, _type: type[str]) -> str:
    if type(value) is not str:
        raise TypeError(f"Expected string, got {type(value).__name__}")
    return value


def _exact_integer(value: JsonValue, _type: type[int]) -> int:
    if type(value) is not int:
        raise TypeError(f"Expected integer, got {type(value).__name__}")
    return value


def _exact_boolean(value: JsonValue, _type: type[bool]) -> bool:
    if type(value) is not bool:
        raise TypeError(f"Expected boolean, got {type(value).__name__}")
    return value


def _string_tuple(value: JsonValue, _type: type[tuple[str, ...]]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"Expected array, got {type(value).__name__}")
    return tuple(_exact_string(item, str) for item in value)


def _converter() -> JsonConverter:
    converter = make_converter(forbid_extra_keys=True, detailed_validation=True)
    converter.register_structure_hook(str, _exact_string)
    converter.register_structure_hook(int, _exact_integer)
    converter.register_structure_hook(bool, _exact_boolean)
    converter.register_structure_hook_func(lambda value_type: value_type == tuple[str, ...], _string_tuple)
    return converter


CONVERTER: Final = _converter()


def decode_json[T](data: bytes | str, model: type[T]) -> T:
    try:
        value: JsonValue = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise JsonCodecError(f"Cannot parse JSON: {error}") from error
    if not isinstance(value, dict):
        raise JsonCodecError("JSON root must be an object.")
    return CONVERTER.structure(value, model)


def read_json[T](path: Path, model: type[T]) -> T:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise JsonCodecError(f"Cannot read JSON at '{path}': {error}") from error
    return decode_json(data, model)


def encode_json[T](value: T) -> bytes:
    return (CONVERTER.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def nested_exception[E: Exception](error: BaseException, expected: type[E]) -> E | None:
    if isinstance(error, expected):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            nested = nested_exception(child, expected)
            if nested is not None:
                return nested
    return None


def validation_message(error: BaseValidationError) -> str:
    return str(error)


def validation_paths(error: BaseValidationError, prefix: tuple[str, ...] = ()) -> tuple[tuple[str, ...], ...]:
    paths: list[tuple[str, ...]] = []
    if isinstance(error, ClassValidationError):
        annotated, unannotated = error.group_exceptions()
        named = ((child, note.name) for child, note in annotated)
    elif isinstance(error, IterableValidationError):
        annotated, unannotated = error.group_exceptions()
        named = ((child, str(note.index)) for child, note in annotated)
    else:
        return (prefix,)
    for child, name in named:
        path = (*prefix, name)
        if isinstance(child, BaseValidationError):
            paths.extend(validation_paths(child, path))
        else:
            paths.append(path)
    if unannotated:
        paths.append(prefix)
    return tuple(paths)
