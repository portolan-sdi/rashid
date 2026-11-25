"""Package data and installed distribution metadata."""

from __future__ import annotations

from importlib import resources
from importlib.resources.abc import Traversable

_NAME = "rashid"


def schemas() -> Traversable:
    """The bundled schema directory."""
    return resources.files(_NAME).joinpath("_schemas")


def version() -> str:
    """The installed version, or unknown without distribution metadata."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as distribution_version

    try:
        return distribution_version(_NAME)
    except PackageNotFoundError:
        return "unknown"
