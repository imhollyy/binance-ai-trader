"""Single-shot CLI tests use a mocked ingest function, never the public internet."""

from __future__ import annotations

import json

import pytest

from binance_ai_trader import market_cli
from binance_ai_trader.market_data import MarketRateLimited
from binance_ai_trader.market_ingest import IngestReport

ARGS = ["--symbol", "BTCUSDT", "--interval", "1m", "--limit", "10"]


def test_cli_requires_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("TRADING_MODE", raising=False)
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    assert market_cli.main(ARGS) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_cli_rejects_nonpaper_execution(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("TRADING_MODE", "live")
    assert market_cli.main(ARGS) == 2
    assert "live trading is disabled" in capsys.readouterr().err


@pytest.mark.parametrize("limit", ["0", "1001", "-1"])
def test_cli_rejects_unbounded_limit(limit: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert market_cli.main(["--symbol", "BTCUSDT", "--interval", "1m", "--limit", limit]) == 2
    assert "Limit must" in capsys.readouterr().err


def test_cli_outputs_only_safe_import_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    calls: list[str] = []

    def fake_ingest(*_args: object, **kwargs: object) -> IngestReport:
        calls.append(str(kwargs["symbol"]))
        return IngestReport("BTCUSDT", "1m", 4, 3, (60_000,))

    monkeypatch.setattr(market_cli, "ingest_once", fake_ingest)
    assert market_cli.main(ARGS) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["received_closed"] == 4
    assert data["inserted_new"] == 3
    assert data["missing_count"] == 1
    assert calls == ["BTCUSDT"]


def test_cli_respects_exchange_rate_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)

    def fake_ingest(*_args: object, **_kwargs: object) -> IngestReport:
        raise MarketRateLimited(429, 30)

    monkeypatch.setattr(market_cli, "ingest_once", fake_ingest)
    assert market_cli.main(ARGS) == 3
    out = capsys.readouterr()
    assert not out.out
    assert "retry_after_seconds=30" in out.err
