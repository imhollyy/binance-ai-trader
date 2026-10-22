"""Auto PAPER pilot must reject unsafe hosts or startup by default (no network)."""

from __future__ import annotations

from collections.abc import Mapping

import pytest
from sqlalchemy import create_engine

from binance_ai_trader.paper_scheduler import OPT_IN_VALUE
from binance_ai_trader.preview_autopilot import (
    CONTROL_APPROVAL,
    PaperPilotRejected,
    change_paper_pilot_mode,
    main,
    validate_preview_pilot,
    paper_pilot_preflight,
)
from binance_ai_trader.staging_auth import password_hash

GOOD: Mapping[str, str] = {
    "PRIVATE_PREVIEW_OPT_IN": "isolated-paper-only",
    "TRADING_MODE": "paper",
    "API_AUTH_MODE": "staging",
    "DATABASE_URL": "postgresql+psycopg://trader_preview:fake@preview-db:5432/trader_preview",
    "STAGING_WEB_ORIGIN": "https://staging.example",
    "STAGING_API_HOST": "staging.example",
    "STAGING_VIEWER_PASSWORD_HASH": password_hash(
        "synthetic-viewer-2026-pilot", salt=b"paperpilotview01"
    ),
    "STAGING_OPERATOR_PASSWORD_HASH": password_hash(
        "synthetic-operator-2026-pilot", salt=b"paperpilotoper01"
    ),
}


def test_autopilot_off_without_all_explicit_gates() -> None:
    assert validate_preview_pilot(GOOD, action="status") == GOOD["DATABASE_URL"]
    assert validate_preview_pilot(GOOD, action="preflight") == GOOD["DATABASE_URL"]
    with pytest.raises(PaperPilotRejected, match="scheduler_opt_in_required"):
        validate_preview_pilot(GOOD, action="run")
    with pytest.raises(PaperPilotRejected, match="local_control_opt_in_required"):
        validate_preview_pilot(GOOD, action="enable")
    with pytest.raises(PaperPilotRejected, match="local_control_opt_in_required"):
        validate_preview_pilot(GOOD, action="disable")
    with pytest.raises(PaperPilotRejected, match="unsupported_action"):
        validate_preview_pilot(GOOD, action="live")


def test_pilot_requires_preview_database_never_local_dev() -> None:
    assert (
        validate_preview_pilot({**GOOD, "PAPER_SCHEDULER_OPT_IN": OPT_IN_VALUE}, action="run")
        == GOOD["DATABASE_URL"]
    )
    assert (
        validate_preview_pilot(
            {**GOOD, "PAPER_AUTOPILOT_CONTROL_OPT_IN": CONTROL_APPROVAL}, action="enable"
        )
        == GOOD["DATABASE_URL"]
    )
    for incorrect in (
        {**GOOD, "DATABASE_URL": "postgresql+psycopg://trader:pw@localhost:5432/trader_dev"},
        {**GOOD, "TRADING_MODE": "live"},
        {**GOOD, "PRIVATE_PREVIEW_OPT_IN": ""},
        {**GOOD, "API_AUTH_MODE": "local"},
        {**GOOD, "ADMIN_TOKEN": "synthetic-local-admin"},
        {**GOOD, "PAPER_WORKER_ENABLED": "true"},
    ):
        with pytest.raises(PaperPilotRejected, match="isolated_preview_required"):
            validate_preview_pilot(incorrect, action="status")
    with pytest.raises(PaperPilotRejected, match="scheduler_opt_in_required"):
        validate_preview_pilot({**GOOD, "PAPER_SCHEDULER_OPT_IN": "bad"}, action="run")


def test_pilot_rejects_non_postgresql_and_requires_action_confirmation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sqlite = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with pytest.raises(PaperPilotRejected, match="postgresql"):
            change_paper_pilot_mode(sqlite, enable=True)
    finally:
        sqlite.dispose()
    monkeypatch.delenv("PRIVATE_PREVIEW_OPT_IN", raising=False)
    assert main(["--action", "enable"]) == 2
    assert "confirmation required" in capsys.readouterr().err
    assert main(["--action", "run", "--confirm-paper-only"]) == 2
    assert "isolated PAPER pilot" in capsys.readouterr().err
    with pytest.raises(SystemExit) as bad_ticks:
        main(["--action", "run", "--confirm-paper-only", "--max-ticks", "11"])
    assert bad_ticks.value.code == 2
    with pytest.raises(PaperPilotRejected, match="postgresql"):
        paper_pilot_preflight(create_engine("sqlite+pysqlite:///:memory:"))
