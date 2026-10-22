"""Local explicit-control PAPER Spot automation pilot for an isolated preview.

This module has ZERO Binance authenticated endpoints. It changes only synthetic
PAPER settings + version + audit, never resets cash/positions/journal. The
scheduler itself remains default-OFF and is started only by a separate opt-in
Compose profile with persisted authorization. Real-money trading is impossible.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from os import environ

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from binance_ai_trader.models import AuditRow, PaperSpotAccountRow, TradingSettingsRow
from binance_ai_trader.paper_execution import PaperExecutionRejected, _locked_state
from binance_ai_trader.paper_scheduler import (
    OPT_IN_VALUE,
    SchedulerStatus,
    run_paper_scheduler,
)
from binance_ai_trader.preview_preflight import verify_preview_environment
from binance_ai_trader.risk_schemas import RiskSettings

CONTROL_APPROVAL = "I_APPROVE_PAPER_AUTOMATION_ONLY"
ALLOWED_SYMBOL = "BTCUSDT"
ALLOWED_INTERVAL = "1m"


class PaperPilotRejected(ValueError):
    """A bounded, non-sensitive refusal without DB mutation."""


def validate_preview_pilot(values: Mapping[str, str], *, action: str) -> str:
    """Require the exact isolated preview DB BEFORE creating any SQL connection."""
    if action not in ("status", "enable", "disable", "run"):
        raise PaperPilotRejected("unsupported_action")
    # The API always denies autonomous operation. A distinct non-HTTP pilot
    # container can have the scheduler opt-in; strip only for origin validation.
    raw = dict(values)
    scheduler_flag = raw.pop("PAPER_SCHEDULER_OPT_IN", "")
    try:
        verify_preview_environment(raw)
    except ValueError as exc:
        raise PaperPilotRejected("isolated_preview_required") from exc
    url = values.get("DATABASE_URL")
    if not url:
        raise PaperPilotRejected("isolated_preview_db_required")
    if action == "run" and scheduler_flag != OPT_IN_VALUE:
        raise PaperPilotRejected("scheduler_opt_in_required")
    if (
        action in ("enable", "disable")
        and values.get("PAPER_AUTOPILOT_CONTROL_OPT_IN") != CONTROL_APPROVAL
    ):
        raise PaperPilotRejected("local_control_opt_in_required")
    return url


def change_paper_pilot_mode(engine: Engine, *, enable: bool) -> int:
    """Atomic account/settings version update after ledger reconciliation.

    The ledger (including realized PnL and fees) is never modified. Existing
    open positions block enable, but disable remains an emergency control.
    """
    if engine.dialect.name != "postgresql" or type(enable) is not bool:
        raise PaperPilotRejected("postgresql_and_explicit_boolean_required")
    with Session(engine) as session, session.begin():
        try:
            current, account, positions, _journal = _locked_state(session)
        except PaperExecutionRejected as exc:
            raise PaperPilotRejected("paper_ledger_not_healthy") from exc
        setting = session.get(TradingSettingsRow, 1)
        if setting is None or account.settings_version != setting.version:
            raise PaperPilotRejected("account_settings_version_mismatch")
        if (
            current.market_type != "spot"
            or current.mode != "paper"
            or current.spot_capital_usdt != 1000
            or account.initial_capital_usdt != 1000
            or current.max_leverage != 1
            or ALLOWED_SYMBOL not in current.allowed_symbols
        ):
            raise PaperPilotRejected("unsupported_paper_pilot_settings")
        if enable:
            if current.bot_enabled or not current.manual_approval:
                raise PaperPilotRejected("pilot_not_in_manual_off_state")
            if any(p.status == "open" for p in positions):
                raise PaperPilotRejected("close_paper_positions_before_automation")
        elif not current.bot_enabled and current.manual_approval:
            # Repeat disabling is a read-only success, no phantom audit/version.
            return setting.version
        elif not current.bot_enabled or current.manual_approval:
            raise PaperPilotRejected("inconsistent_paper_mode")
        updated = RiskSettings.model_validate(
            {**current.model_dump(mode="json"),
             "bot_enabled": enable, "manual_approval": not enable}
        )
        next_version = setting.version + 1
        setting.version = next_version
        setting.data = updated.model_dump(mode="json")
        setting.modified_at = datetime.now(UTC)
        account.settings_version = next_version
        session.add(
            AuditRow(
                settings_id=1,
                version=next_version,
                action="settings_updated",
                data=setting.data,
                created_at=datetime.now(UTC),
            )
        )
        return next_version


def paper_pilot_status(engine: Engine) -> str:
    """Read-only status; never mistake permission for a running scheduler."""
    if engine.dialect.name != "postgresql":
        raise PaperPilotRejected("postgresql_required")
    with Session(engine) as session:
        settings = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        if settings is None or account is None:
            raise PaperPilotRejected("paper_account_not_initialized")
        rules = RiskSettings.model_validate(settings.data)
        if account.settings_version != settings.version:
            raise PaperPilotRejected("account_settings_version_mismatch")
        if rules.bot_enabled and not rules.manual_approval:
            return "AUTO_PAPER_PERMITTED_NOT_RUNNING_VERIFIED"
        if not rules.bot_enabled and rules.manual_approval:
            return "MANUAL_PAPER_ONLY_SCHEDULER_OFF"
        raise PaperPilotRejected("inconsistent_paper_mode")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated preview PAPER automation; default OFF")
    parser.add_argument(
        "--action", choices=["status", "enable", "disable", "run"], default="status"
    )
    parser.add_argument("--confirm-paper-only", action="store_true")
    args = parser.parse_args(argv)

    # A database operation is not even attempted if the environment isn't
    # a private preview. No local developer DB, real DB or Cloudflare action.
    if args.action != "status" and not args.confirm_paper_only:
        print("REJECTED: explicit PAPER-only confirmation required", file=sys.stderr)
        return 2
    try:
        db_url = validate_preview_pilot(environ, action=args.action)
        engine = create_engine(db_url, pool_pre_ping=True, pool_timeout=5)
        try:
            if args.action in ("enable", "disable"):
                version = change_paper_pilot_mode(engine, enable=args.action == "enable")
                print(f"PAPER_MODE_UPDATED version={version} (no ledger reset or exchange order)")
            elif args.action == "status":
                print("PAPER_STATUS " + paper_pilot_status(engine))
            else:
                if paper_pilot_status(engine) != "AUTO_PAPER_PERMITTED_NOT_RUNNING_VERIFIED":
                    raise PaperPilotRejected("explicit_account_mode_enable_required")
                result = run_paper_scheduler(
                    engine, enabled=True,
                    symbol=ALLOWED_SYMBOL, interval=ALLOWED_INTERVAL,
                )
                print(f"PAPER_PILOT_ENDED status={result.status.value} ticks={result.ticks}")
                return (
                    0
                    if result.status in (SchedulerStatus.STOPPED, SchedulerStatus.DISABLED)
                    else 1
                )
        finally:
            engine.dispose()
    except (PaperPilotRejected, SQLAlchemyError, ValueError):
        print(
            "REJECTED: isolated PAPER pilot or database safety validation failed",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
