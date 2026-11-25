"""PTL-DAT-011: each overview level reduces the one before it by 2–10 (#327)."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rashid_core.data.formats.raster import _overview_reduction

pytestmark = pytest.mark.unit

# gdal_translate -of COG on a 4000x1001 raster: GDAL rounds 1001 -> 500 and 125 -> 62.
_GDAL_PYRAMID = [(1001, 4000), (500, 2000), (250, 1000), (125, 500), (62, 250)]


@pytest.mark.parametrize(("previous", "current"), list(itertools.pairwise(_GDAL_PYRAMID)))
def test_gdal_pyramid_rounding_down_passes(
    previous: tuple[int, int], current: tuple[int, int]
) -> None:
    assert _overview_reduction(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ((1001, 4000), (501, 2000)),  # rounded up
        ((1000, 1000), (100, 100)),  # exactly 10
        ((1009, 1009), (100, 100)),  # 10, rounded down
        ((1, 4000), (1, 2000)),  # saturated axis
    ],
)
def test_reductions_within_two_to_ten_pass(
    previous: tuple[int, int], current: tuple[int, int]
) -> None:
    assert _overview_reduction(previous, current)


@pytest.mark.parametrize(
    ("previous", "current"),
    [
        ((1000, 1000), (600, 600)),  # factor below 2
        ((1001, 4000), (502, 2000)),  # 1.99, past rounding
        ((1100, 1100), (100, 100)),  # factor 11
        ((1001, 4000), (498, 2000)),  # height off by two pixels
        ((1000, 1000), (500, 502)),  # one axis off by two pixels
        ((1000, 1000), (1000, 1000)),  # no reduction
        ((1, 1), (1, 1)),  # no reduction, saturated
    ],
)
def test_reductions_outside_two_to_ten_fail(
    previous: tuple[int, int], current: tuple[int, int]
) -> None:
    assert not _overview_reduction(previous, current)


Rounding = Callable[[float], int]
_rounding: st.SearchStrategy[Rounding] = st.sampled_from([math.floor, math.ceil])


@given(
    st.integers(10, 100_000),
    st.integers(10, 100_000),
    st.integers(2, 10),
    _rounding,
    _rounding,
)
def test_floor_or_ceil_of_any_factor_passes(
    height: int, width: int, scale: int, round_height: Rounding, round_width: Rounding
) -> None:
    current = (round_height(height / scale), round_width(width / scale))
    assert _overview_reduction((height, width), current)


@given(st.integers(1_100, 100_000), st.integers(11, 100), _rounding)
def test_factor_above_ten_fails(size: int, scale: int, rounding: Rounding) -> None:
    after = rounding(size / scale)
    assert not _overview_reduction((size, size), (after, after))


@given(st.integers(10, 100_000), st.integers(0, 10))
def test_factor_below_two_fails(size: int, extra: int) -> None:
    after = min(size - 1, size // 2 + 2 + extra)
    assert not _overview_reduction((size, size), (after, after))


@given(st.integers(20, 100_000), st.integers(2, 10), st.integers(2, 50), _rounding)
def test_one_axis_off_by_two_or_more_fails(
    size: int, scale: int, offset: int, rounding: Rounding
) -> None:
    after = rounding(size / scale)
    assert not _overview_reduction((size, size), (after, after + offset))
