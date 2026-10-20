"""Gap scanning is bounded and never makes exchange requests."""

from __future__ import annotations

import pytest

from binance_ai_trader.market_store import find_missing_open_times


def test_gap_scan_reports_only_absent_utc_opens() -> None:
    assert find_missing_open_times(
        [0, 120_000, 180_000], start_ms=0, end_ms=180_000, interval="1m"
    ) == [60_000]


def test_gap_scan_is_empty_when_all_intervals_present() -> None:
    assert (
        find_missing_open_times([0, 300_000, 600_000], start_ms=0, end_ms=600_000, interval="5m")
        == []
    )


@pytest.mark.parametrize(
    ("start", "end", "interval"),
    [
        (10, 60_000, "1m"),
        (60_000, 0, "1m"),
        (-60_000, 0, "1m"),
        (0, 1_500 * 60_000, "1m"),
        (0, 15_000, "15s"),
    ],
)
def test_unbounded_or_unaligned_scan_rejected(start: int, end: int, interval: str) -> None:
    with pytest.raises(ValueError):
        find_missing_open_times([], start_ms=start, end_ms=end, interval=interval)


def test_unexpected_observed_opens_rejected() -> None:
    with pytest.raises(ValueError):
        find_missing_open_times([30_000], start_ms=0, end_ms=120_000, interval="1m")
