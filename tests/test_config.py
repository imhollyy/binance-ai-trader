"""Fail-closed configuration contract."""

import pytest

from binance_ai_trader.config import RunMode, Settings


def test_default_mode_is_paper() -> None:
    assert Settings.from_env({}).mode is RunMode.PAPER


@pytest.mark.parametrize("value", ["paper", "PAPER", " paper "])
def test_paper_mode_allowed(value: str) -> None:
    assert Settings.from_env({"TRADING_MODE": value}).mode is RunMode.PAPER


@pytest.mark.parametrize("value", ["live", "spot", "futures", "margin", "", "paper,live"])
def test_non_paper_mode_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="live trading is disabled"):
        Settings.from_env({"TRADING_MODE": value})
