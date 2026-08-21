"""Pinboard coordination primitives."""

from importlib.metadata import version
from typing import Final

from repo_work.identity import DISTRIBUTION_NAME

__version__: Final = version(DISTRIBUTION_NAME)
