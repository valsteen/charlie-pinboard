"""Concrete stdout effects shared by installed command presenters."""

import sys

import msgspec


def write_json[T](value: T) -> None:
    """Write one canonical, human-readable JSON value and nothing else."""

    encoded = msgspec.json.encode(value, order="sorted")
    sys.stdout.write(msgspec.json.format(encoded, indent=2).decode() + "\n")
