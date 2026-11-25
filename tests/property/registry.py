"""Shared contract for the property tests: one breaker per check ID.

A breaker takes a valid catalog tree that a strategy generated and wrote to
``root``. It edits the tree so that exactly its rule is violated, and returns
the options to validate with, or a :class:`Target` when the tree must be
validated from another location, such as a URL a fetcher serves it under.
``test_rules_fire.py`` asserts that the check ID appears in the report. The completeness gate there requires
``set(BREAKERS) | set(UNBREAKABLE) == set(CHECKS)``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from rashid_core import ValidationOptions


class Target(NamedTuple):
    """Validate ``location`` with ``options`` instead of the tree's root path."""

    location: str | Path
    options: ValidationOptions


Breaker = Callable[[Path], ValidationOptions | Target | None]

BREAKERS: dict[str, Breaker] = {}

#: Check IDs no catalog contents can trigger, each with the reason.
UNBREAKABLE: dict[str, str] = {}


def breaks(check_id: str) -> Callable[[Breaker], Breaker]:
    """Register ``fn`` as the breaker for ``check_id``; a duplicate ID fails."""

    def register(fn: Breaker) -> Breaker:
        assert check_id not in BREAKERS, f"duplicate breaker for {check_id}"
        BREAKERS[check_id] = fn
        return fn

    return register
