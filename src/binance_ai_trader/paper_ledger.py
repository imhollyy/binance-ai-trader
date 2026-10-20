"""PostgreSQL bootstrap for the local PAPER Spot account; no trade execution."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    TradingSettingsRow,
)
from binance_ai_trader.risk_schemas import RiskSettings


class PaperAccountRejected(ValueError):
    """Account bootstrap denied without changing the ledger."""


@dataclass(frozen=True, slots=True)
class PaperAccountSnapshot:
    initial_capital_usdt: Decimal
    cash_usdt: Decimal
    settings_version: int


def ensure_paper_spot_account(session: Session) -> PaperAccountSnapshot:
    """Create once from persisted settings, or read without resetting cash.

    The caller supplies a fresh Session. This function owns a short transaction,
    locks the stored settings row, and rejects a changed Spot budget.
    """

    if session.get_bind().dialect.name != "postgresql":
        raise PaperAccountRejected("postgresql_required")
    with session.begin():
        settings_row = session.scalar(
            select(TradingSettingsRow).where(TradingSettingsRow.id == 1).with_for_update()
        )
        if settings_row is None:
            raise PaperAccountRejected("persisted_paper_settings_required")
        try:
            settings = RiskSettings.model_validate(settings_row.data)
        except ValidationError as exc:
            raise PaperAccountRejected("invalid_persisted_paper_settings") from exc
        if settings.mode != "paper" or settings.market_type != "spot":
            raise PaperAccountRejected("spot_paper_only")
        capital = Decimal(settings.spot_capital_usdt)
        if capital <= 0:
            raise PaperAccountRejected("paper_spot_capital_missing")

        created = session.execute(
            insert(PaperSpotAccountRow)
            .values(
                id=1,
                settings_version=settings_row.version,
                initial_capital_usdt=capital,
                cash_usdt=capital,
            )
            .on_conflict_do_nothing(index_elements=["id"])
            .returning(PaperSpotAccountRow.id)
        ).scalar_one_or_none()
        account = session.scalar(
            select(PaperSpotAccountRow).where(PaperSpotAccountRow.id == 1).with_for_update()
        )
        if account is None:
            raise PaperAccountRejected("paper_spot_account_missing")
        if (
            account.mode != "paper"
            or account.market_type != "spot"
            or account.initial_capital_usdt != capital
        ):
            raise PaperAccountRejected("paper_spot_capital_changed")
        if created is not None:
            session.add(
                PaperSpotJournalRow(
                    account_id=1,
                    event_id="initial-capital",
                    idempotency_key="initial-capital",
                    event_type="account_opened",
                    cash_delta_usdt=capital,
                    balance_after_usdt=capital,
                )
            )
            session.flush()
        else:
            opening = session.scalar(
                select(PaperSpotJournalRow).where(
                    PaperSpotJournalRow.account_id == 1,
                    PaperSpotJournalRow.event_id == "initial-capital",
                )
            )
            if (
                opening is None
                or opening.event_type != "account_opened"
                or opening.cash_delta_usdt != capital
                or opening.balance_after_usdt != capital
            ):
                raise PaperAccountRejected("paper_spot_opening_event_missing")
        return PaperAccountSnapshot(
            initial_capital_usdt=account.initial_capital_usdt,
            cash_usdt=account.cash_usdt,
            settings_version=account.settings_version,
        )
