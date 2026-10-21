"""Explicit *local Docker exec only* onboarding for an isolated 1,000 USDT PAPER account.

Never register this as an HTTP endpoint or call it at container startup.
This tool cannot connect to Binance, run a worker or reset a ledger.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from os import environ

from pydantic import ValidationError
from sqlalchemy import Engine, create_engine, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from binance_ai_trader.models import AuditRow, TradingSettingsRow
from binance_ai_trader.paper_ledger import (
    PaperAccountRejected,
    PaperAccountSnapshot,
    ensure_paper_spot_account,
)
from binance_ai_trader.preview_preflight import verify_preview_environment
from binance_ai_trader.risk_schemas import RiskSettings

CAPITAL_USDT = 1000


class PreviewBootstrapRejected(ValueError):
    """Unsafe or incompatible initialization requested; do not modify data."""


def check_local_preview(values: Mapping[str, str]) -> str:
    """Fail closed before any database connection, even when called without CLI."""
    try:
        verify_preview_environment(values)
    except ValueError as exc:
        raise PreviewBootstrapRejected("isolated_preview_required") from exc
    if values.get("API_AUTH_MODE") != "staging":
        raise PreviewBootstrapRejected("staging_only")
    url = values.get("DATABASE_URL")
    if not url:
        raise PreviewBootstrapRejected("isolated_preview_database_required")
    return url


def bootstrap_once(engine: Engine) -> PaperAccountSnapshot:
    """Idempotent: create only missing safe defaults, then use tested PAPER ledger."""
    if engine.dialect.name != "postgresql":
        raise PreviewBootstrapRejected("postgresql_required")
    settings = RiskSettings()
    if settings.spot_capital_usdt != CAPITAL_USDT:
        raise PreviewBootstrapRejected("default_capital_mismatch")

    # Separate transaction from the existing ledger's transaction owner.
    # A failure after settings creation is safe to retry; no account is reset.
    with Session(engine) as session, session.begin():
        created = session.execute(
            insert(TradingSettingsRow)
            .values(
                id=1,
                version=1,
                data=settings.model_dump(mode="json"),
                modified_at=datetime.now(UTC),
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(TradingSettingsRow.id)
        ).scalar_one_or_none()
        existing = session.scalar(
            select(TradingSettingsRow).where(TradingSettingsRow.id == 1).with_for_update()
        )
        if existing is None:
            raise PreviewBootstrapRejected("settings_missing")
        try:
            current = RiskSettings.model_validate(existing.data)
        except ValidationError as exc:
            raise PreviewBootstrapRejected("invalid_existing_settings") from exc
        if (
            current.mode != "paper"
            or current.market_type != "spot"
            or current.bot_enabled
            or current.spot_capital_usdt != CAPITAL_USDT
            or current.manual_approval is not True
            or current.max_leverage != 1
        ):
            raise PreviewBootstrapRejected("incompatible_existing_settings")
        if created is not None:
            session.add(
                AuditRow(
                    settings_id=1,
                    version=1,
                    action="settings_updated",
                    data=existing.data,
                    created_at=datetime.now(UTC),
                )
            )

    with Session(engine) as session:
        return ensure_paper_spot_account(session)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local isolated PAPER Spot initialization; never touches real Binance"
    )
    parser.add_argument("--capital-usdt", required=True, type=int, choices=[CAPITAL_USDT])
    parser.add_argument("--confirm-paper-only", action="store_true")
    args = parser.parse_args(argv)
    if not args.confirm_paper_only:
        print("REJECTED: explicit --confirm-paper-only is required", file=sys.stderr)
        return 2
    try:
        database_url = check_local_preview(environ)
        engine = create_engine(database_url, pool_pre_ping=True)
        try:
            snapshot = bootstrap_once(engine)
        finally:
            engine.dispose()
    except (PreviewBootstrapRejected, PaperAccountRejected):
        print("REJECTED: incompatible or unsafe PAPER account configuration", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        # Do not expose the DB URL or credentials in an exception message.
        print("ERROR: isolated PAPER preview database unavailable", file=sys.stderr)
        return 1
    print(
        f"PAPER_ACCOUNT_READY initial_capital_usdt={snapshot.initial_capital_usdt} "
        f"cash_usdt={snapshot.cash_usdt} settings_version={snapshot.settings_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
