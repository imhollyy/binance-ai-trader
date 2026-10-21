"""Isolated PAPER onboarding must never target non-preview DBs or reset account."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from sqlalchemy import create_engine

from binance_ai_trader.preview_account_bootstrap import (
    PreviewBootstrapRejected,
    bootstrap_once,
    check_local_preview,
    main,
)
from binance_ai_trader.staging_auth import password_hash

GOOD: Mapping[str, str] = {
    "PRIVATE_PREVIEW_OPT_IN": "isolated-paper-only",
    "TRADING_MODE": "paper",
    "API_AUTH_MODE": "staging",
    "DATABASE_URL": "postgresql+psycopg://trader_preview:fake@preview-db:5432/trader_preview",
    "STAGING_WEB_ORIGIN": "https://preview.staging.example",
    "STAGING_API_HOST": "preview.staging.example",
    "STAGING_VIEWER_PASSWORD_HASH": password_hash(
        "synthetic-viewer-password-2026", salt=b"preview-view-001"
    ),
    "STAGING_OPERATOR_PASSWORD_HASH": password_hash(
        "synthetic-operator-password-2026", salt=b"preview-oper-001"
    ),
}


def test_bootstrap_config_only_accepts_isolated_staging_db() -> None:
    assert check_local_preview(GOOD) == GOOD["DATABASE_URL"]


@pytest.mark.parametrize(
    ("field", "invalid"),
    (
        ("DATABASE_URL", "postgresql+psycopg://trader_preview:fake@prod-db:5432/trader_preview"),
        ("DATABASE_URL", "postgresql+psycopg://trader_preview:fake@localhost:5432/trader_preview"),
        ("API_AUTH_MODE", "local"),
        ("TRADING_MODE", "live"),
        ("PRIVATE_PREVIEW_OPT_IN", ""),
        ("PAPER_WORKER_ENABLED", "true"),
        ("PAPER_SCHEDULER_OPT_IN", "enabled"),
    ),
)
def test_bootstrap_rejects_non_preview_environment(field: str, invalid: str) -> None:
    with pytest.raises(PreviewBootstrapRejected):
        check_local_preview({**GOOD, field: invalid})


def test_bootstrap_rejects_sqlite_without_mutating_db() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(PreviewBootstrapRejected, match="postgresql_required"):
            bootstrap_once(engine)
    finally:
        engine.dispose()


def test_cli_requires_explicit_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--capital-usdt", "1000"]) == 2
    assert "confirm-paper-only" in capsys.readouterr().err


def test_cli_rejects_wrong_amount_before_database(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as error:
        main(["--capital-usdt", "2000", "--confirm-paper-only"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
