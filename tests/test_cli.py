"""CLI behavior is safe and testable without an exchange connection."""

import pytest

from binance_ai_trader.cli import main


def test_cli_reports_paper_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TRADING_MODE", raising=False)
    assert main(["--status"]) == 0
    assert "mode=paper | live_trading=disabled" in capsys.readouterr().out


def test_cli_fails_closed_for_live_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TRADING_MODE", "live")
    assert main(["--status"]) == 2
    assert "live trading is disabled" in capsys.readouterr().err
