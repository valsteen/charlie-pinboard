"""Pinboard coordination primitives."""

from importlib.metadata import version
from typing import Final

from charlie_pinboard.identity import DISTRIBUTION_NAME

__version__: Final = version(DISTRIBUTION_NAME)
