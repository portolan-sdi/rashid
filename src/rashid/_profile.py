"""The Portolan profile versions this build bundles, and the spec commit behind them.

It reads only the package data directory, so the CLI, the rules, and the
schema pass can ask which version is newest without loading a schema.
"""

from __future__ import annotations

import re
from functools import lru_cache
from importlib import resources

from rashid import _json

PROFILE_URI_PREFIX = "https://schemas.portolan-sdi.org/portolan/"
_PROFILE_URI = re.compile(
    r"https://schemas\.portolan-sdi\.org/portolan/(v[0-9]+\.[0-9]+\.[0-9]+)/schema\.json"
)


def profile_version_key(version: str) -> tuple[int, ...]:
    """Sort key for a ``vX.Y.Z`` version: numeric, so v0.10.0 follows v0.9.0."""
    return tuple(int(part) for part in version.removeprefix("v").split("."))


def profile_uri(version: str) -> str:
    """The schema URI a catalog declares for ``version``."""
    return f"{PROFILE_URI_PREFIX}{version}/schema.json"


def profile_version(uri: str) -> str | None:
    """The ``vX.Y.Z`` in a profile schema URI, or None for another URI."""
    match = _PROFILE_URI.fullmatch(uri)
    return match.group(1) if match else None


@lru_cache(maxsize=1)
def bundled_profile_versions() -> tuple[str, ...]:
    """The profile schema versions in the wheel, oldest first."""
    root = resources.files("rashid").joinpath("_schemas/portolan")
    found = [d.name for d in root.iterdir() if d.joinpath("schema.json").is_file()]
    return tuple(sorted(found, key=profile_version_key))


def newest_profile_version() -> str:
    """The newest bundled profile version: the requirements rashid applies."""
    return bundled_profile_versions()[-1]


@lru_cache(maxsize=1)
def spec_commit() -> str | None:
    """The portolan-spec commit the bundled schemas and fixtures came from."""
    ref = resources.files("rashid").joinpath("_schemas/spec-ref.json")
    if not ref.is_file():
        return None
    commit = _json.loads(ref.read_bytes()).get("commit")
    return commit if isinstance(commit, str) else None
