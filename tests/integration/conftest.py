from __future__ import annotations

import pytest


@pytest.fixture
def zero_ordering_size_floor_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disable the spec's 5 MB ordering floor (PORTO-FMT-058) for modules whose
    kilobyte fixtures pin judged ordering verdicts. Opt in with
    ``pytest.mark.usefixtures`` in the module's ``pytestmark``."""
    from tests.integration import _data_assets as assets

    assets.zero_ordering_size_floor(monkeypatch)
