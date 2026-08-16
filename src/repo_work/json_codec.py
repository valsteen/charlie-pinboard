from typing import Final

from cattrs.errors import BaseValidationError, ClassValidationError, IterableValidationError
from cattrs.preconf.json import JsonConverter, make_converter

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


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


def register_json_array[T](array_type: type[tuple[T, ...]], item_type: type[T]) -> None:
    def structure_array(value: JsonValue, _requested_type: type[tuple[T, ...]]) -> tuple[T, ...]:
        if not isinstance(value, list):
            raise TypeError(f"Expected array, got {type(value).__name__}")
        return tuple(CONVERTER.structure(item, item_type) for item in value)

    CONVERTER.register_structure_hook_func(lambda value_type: value_type == array_type, structure_array)


def _converter() -> JsonConverter:
    converter = make_converter(forbid_extra_keys=True, detailed_validation=True, omit_if_default=True)
    converter.register_structure_hook(str, _exact_string)
    converter.register_structure_hook(int, _exact_integer)
    converter.register_structure_hook(bool, _exact_boolean)
    return converter


CONVERTER: Final = _converter()
register_json_array(tuple[str, ...], str)


def nested_exception[E: Exception](error: BaseException, expected: type[E]) -> E | None:
    if isinstance(error, expected):
        return error
    if isinstance(error, BaseExceptionGroup):
        for child in error.exceptions:
            nested = nested_exception(child, expected)
            if nested is not None:
                return nested
    return None


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
