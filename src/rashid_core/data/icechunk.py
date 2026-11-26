"""Read an Icechunk repository's Zarr hierarchy at the tip of ``main``.

incubating/zarr.md, Icechunk: the profile's rules apply to the hierarchy at
the tip of ``main``. The keys of a repository are not Zarr keys, so the
hierarchy is read through the Icechunk library, from the optional
``icechunk`` extra. The library sends its own requests, as GDAL does; the
pass's reader only decides whether the repository is in scope and where its
``repo`` file is.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from rashid_core._href import href_suffix
from rashid_core._http import user_agent
from rashid_core.data.reader import Locator

if TYPE_CHECKING:
    import icechunk

#: Attempts per request, and the time one may take. The library's defaults
#: retry for minutes, and an unreachable host must cost one short wait.
_MAX_TRIES = 2
_TIMEOUT_MS = 30_000

#: The repository's file that names its format version and its branches.
REPO_FILE = "repo"

# A reader of one key at the tip of main: its bytes, or None when absent.
KeyReader = Callable[[str], bytes | None]


def spec_version(located: Locator) -> int | None:
    """The repository's Icechunk format version, or ``None`` when there is none.

    ``located`` is where the reader found the ``repo`` file. Raises
    ``ImportError`` without the Icechunk library, and ``OSError`` when the
    repository could not be read.
    """
    import icechunk

    try:
        version = icechunk.Repository.fetch_spec_version(_storage(located), _settings())
    except icechunk.IcechunkError as exc:
        raise OSError(str(exc)) from exc
    return None if version is None else int(version)


def main_reader(located: Locator) -> KeyReader:
    """A reader of the keys at the tip of ``main``. Raises as :func:`spec_version` does."""
    import asyncio

    import icechunk
    from zarr.core.buffer import default_buffer_prototype

    try:
        repository = icechunk.Repository.open(
            _storage(located), config=icechunk.RepositoryConfig(storage=_settings())
        )
        store = repository.readonly_session(branch="main").store
    except icechunk.IcechunkError as exc:
        raise OSError(str(exc)) from exc

    def read(key: str) -> bytes | None:
        try:
            # The data pass runs each object on a worker thread with no event loop.
            buffer = asyncio.run(store.get(key, prototype=default_buffer_prototype()))
        except icechunk.IcechunkError as exc:
            raise OSError(str(exc)) from exc
        return None if buffer is None else buffer.to_bytes()

    return read


def _storage(located: Locator) -> icechunk.Storage:
    import icechunk

    source = located.source
    if not located.is_remote:
        return icechunk.local_filesystem_storage(str(Path(source).parent))
    root = source[: len(source) - len(href_suffix(source))].removesuffix(f"/{REPO_FILE}")
    return icechunk.http_storage(root, headers={"User-Agent": user_agent()})


def _settings() -> icechunk.StorageSettings:
    import icechunk

    return icechunk.StorageSettings(
        retries=icechunk.StorageRetriesSettings(max_tries=_MAX_TRIES),
        timeouts=icechunk.StorageTimeoutSettings(operation_timeout_ms=_TIMEOUT_MS),
    )
