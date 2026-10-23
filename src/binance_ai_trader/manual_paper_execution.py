"""One confirmed manual PAPER Spot simulation on an isolated local preview.

No exchange credentials, order endpoints, scheduler or caller-supplied prices.
The persistent PostgreSQL advisory lock spans public observation and settlement
so retrying a request ID replays the same journal receipt, even across restarts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_DOWN, Context, Decimal, Inexact, localcontext
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotObservedQuoteRow,
    PaperSpotPositionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_entry_gate import require_atomic_riskguard_authorization
from binance_ai_trader.paper_execution import (
    PaperExecutionRejected,
    PaperTradeReceipt,
    _money,
    close_paper_spot,
    open_paper_spot,
)
from binance_ai_trader.paper_spot import PaperCosts, SpotBuyIntent
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservationClient,
    validate_public_observation,
)
from binance_ai_trader.risk_schemas import RiskSettings

LOCK_KEY = 7_812_243_021  # separate from the worker's process-scoped lock
COSTS = PaperCosts()
# Derived fee (3 decimals) and slippage (4 decimals) must fit the ledger's
# exact 18-decimal storage: price 6 places + quantity 5 + slippage 4 + fee 3 = 18.
# Fail closed on more precise public references rather than silently round them.
PRICE_SCALE = Decimal("0.000001")
QUANTITY_SCALE = Decimal("0.00001")


class ManualPaperRequest(BaseModel):
    """No reference_price/fee/quantity is trusted from browser JSON."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    action: Literal["buy", "sell"]
    symbol: Literal["BTCUSDT", "ETHUSDT"]
    request_id: UUID
    confirmation: Literal["CONFIRM_MANUAL_PAPER_ONLY"]
    budget_usdt: Decimal | None = Field(default=None, ge=10, le=100, decimal_places=2)
    stop_distance_pct: Decimal | None = Field(default=None, ge=1, le=3, decimal_places=2)

    @model_validator(mode="after")
    def validate_action(self) -> ManualPaperRequest:
        if self.request_id.version != 4:
            raise ValueError("uuid4_request_required")
        if self.action == "buy":
            if self.budget_usdt is None or self.stop_distance_pct is None:
                raise ValueError("buy_budget_and_stop_required")
        elif self.budget_usdt is not None or self.stop_distance_pct is not None:
            raise ValueError("sell_must_not_supply_budget_or_stop")
        return self


@dataclass(frozen=True)
class ManualPaperResult:
    receipt: PaperTradeReceipt
    reference_price: Decimal
    observed_at_ms: int


def _canonical(request: ManualPaperRequest) -> str:
    """Stable identity protects reused nonces with conflicting buy parameters."""
    data = {
        "v": 1,
        "action": request.action,
        "symbol": request.symbol,
        "nonce": str(request.request_id),
        "budget": str(request.budget_usdt) if request.budget_usdt is not None else None,
        "stop_pct": (
            str(request.stop_distance_pct) if request.stop_distance_pct is not None else None
        ),
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _existing(engine: Engine, request: ManualPaperRequest) -> ManualPaperResult | None:
    key = "manual-" + request.request_id.hex
    with Session(engine) as session:
        journal = session.scalar(
            select(PaperSpotJournalRow).where(PaperSpotJournalRow.idempotency_key == key)
        )
        if journal is None:
            return None
        position = session.get(PaperSpotPositionRow, journal.position_id)
        proof = session.scalar(
            select(PaperSpotObservedQuoteRow).where(
                PaperSpotObservedQuoteRow.journal_id == journal.id
            )
        )
        if position is None or proof is None:
            raise PaperExecutionRejected("manual_paper_audit_missing")
        if (
            journal.event_type != ("spot_buy" if request.action == "buy" else "spot_sell")
            or position.symbol != request.symbol
            or proof.symbol != request.symbol
        ):
            raise PaperExecutionRejected("manual_paper_idempotency_conflict")
        if request.action == "buy" and position.trade_id != "manual_" + _canonical(request)[:40]:
            raise PaperExecutionRejected("manual_paper_idempotency_conflict")
        return ManualPaperResult(
            PaperTradeReceipt(
                event_id=journal.event_id,
                idempotency_key=journal.idempotency_key,
                trade_id=position.trade_id,
                event_type=journal.event_type,
                cash_after_usdt=journal.balance_after_usdt,
                fee_usdt=journal.fee_usdt,
                realized_pnl_delta_usdt=journal.realized_pnl_delta_usdt,
            ),
            reference_price=proof.reference_price,
            observed_at_ms=proof.received_at_ms,
        )


def _config_allowed(
    session: Session, account: PaperSpotAccountRow, request: ManualPaperRequest
) -> None:
    row = session.get(TradingSettingsRow, 1)
    if row is None:
        raise PaperExecutionRejected("manual_paper_settings_missing")
    settings = RiskSettings.model_validate(row.data)
    if (
        settings.mode != "paper"
        or settings.market_type != "spot"
        or settings.bot_enabled
        or not settings.manual_approval
        or request.symbol not in settings.allowed_symbols
        or account.mode != "paper"
        or account.market_type != "spot"
        or account.initial_capital_usdt != Decimal("1000")
        or account.settings_version != row.version
        or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
    ):
        raise PaperExecutionRejected("manual_paper_settings_not_approved")


def run_manual_paper_once(
    engine: Engine,
    request: ManualPaperRequest,
    *,
    observer: PublicSpotObservationClient | None = None,
) -> ManualPaperResult:
    """Synchronous user action; a fresh public sample is required for new events.

    Same request nonce returns the original persisted receipt *without*
    recalculating a historical fill from a new ticker. No auto worker flag.
    """
    if engine.dialect.name != "postgresql":
        raise PaperExecutionRejected("postgresql_required")
    if not isinstance(request, ManualPaperRequest):
        raise PaperExecutionRejected("manual_request_required")
    price_source = PublicSpotObservationClient() if observer is None else observer
    if type(price_source) is not PublicSpotObservationClient:
        raise PaperExecutionRejected("trusted_public_observer_required")

    with engine.connect() as lease:
        acquired = bool(
            lease.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": LOCK_KEY}
            ).scalar_one()
        )
        lease.commit()
        if not acquired:
            raise PaperExecutionRejected("manual_paper_busy")
        try:
            replay = _existing(engine, request)
            if replay is not None:
                return replay
            if request.action == "buy":
                require_atomic_riskguard_authorization()
            with Session(engine) as session:
                account = session.get(PaperSpotAccountRow, 1)
                if account is None:
                    raise PaperExecutionRejected("paper_account_required")
                _config_allowed(session, account, request)
                open_items = list(
                    session.scalars(
                        select(PaperSpotPositionRow).where(
                            PaperSpotPositionRow.account_id == 1,
                            PaperSpotPositionRow.status == "open",
                        )
                    )
                )
                if request.action == "sell":
                    if len(open_items) != 1 or open_items[0].symbol != request.symbol:
                        raise PaperExecutionRejected("manual_open_position_required")
                    trade_id = open_items[0].trade_id
                else:
                    if open_items:
                        raise PaperExecutionRejected("manual_position_already_open")
                    trade_id = "manual_" + _canonical(request)[:40]
                session.rollback()

            sample = price_source.observe(symbol=request.symbol, interval="1m", limit=100)
            validate_public_observation(sample, now_ms=price_source.now_ms())
            try:
                with localcontext(Context(prec=64, traps=[Inexact])):
                    reference = sample.reference_price.quantize(PRICE_SCALE)
            except Inexact as exc:
                raise PaperExecutionRejected("unsupported_public_reference_precision") from exc
            reference = _money(reference)

            def locked_verify(
                session: Session,
                _settings: RiskSettings,
                account: PaperSpotAccountRow,
                positions: list[PaperSpotPositionRow],
                _events: list[PaperSpotJournalRow],
            ) -> None:
                if request.action == "buy":
                    require_atomic_riskguard_authorization()
                _config_allowed(session, account, request)
                selected = [p for p in positions if p.status == "open"]
                if request.action == "sell":
                    if len(selected) != 1 or selected[0].trade_id != trade_id:
                        raise PaperExecutionRejected("manual_position_changed")
                elif selected:
                    raise PaperExecutionRejected("manual_position_changed")
                validate_public_observation(sample, now_ms=price_source.now_ms())

            def audit(session: Session, journal: PaperSpotJournalRow) -> None:
                session.add(
                    PaperSpotObservedQuoteRow(
                        journal_id=journal.id,
                        symbol=request.symbol,
                        interval=sample.interval,
                        source=sample.source,
                        signal_close_time_ms=sample.signal_close_time_ms,
                        requested_at_ms=sample.requested_at_ms,
                        received_at_ms=sample.received_at_ms,
                        reference_price=reference,
                        fee_bps=COSTS.fee_bps,
                        slippage_bps=COSTS.slippage_bps,
                        spread_bps=COSTS.spread_bps,
                    )
                )

            key = "manual-" + request.request_id.hex
            with Session(engine) as session:
                if request.action == "buy":
                    assert request.budget_usdt is not None
                    assert request.stop_distance_pct is not None
                    with localcontext(Context(prec=64)):
                        fee_factor = Decimal(1) + Decimal(COSTS.fee_bps) / Decimal(10_000)
                        slip_factor = Decimal(1) + Decimal(COSTS.slippage_bps) / Decimal(10_000)
                        quantity = (
                            request.budget_usdt / (reference * fee_factor * slip_factor)
                        ).quantize(QUANTITY_SCALE, rounding=ROUND_DOWN)
                        stop = (
                            reference * (Decimal(1) - request.stop_distance_pct / 100)
                        ).quantize(PRICE_SCALE, rounding=ROUND_DOWN)
                    if quantity <= 0 or stop <= 0:
                        raise PaperExecutionRejected("invalid_manual_paper_allocation")
                    receipt = open_paper_spot(
                        session,
                        SpotBuyIntent(trade_id, request.symbol, reference, quantity, stop),
                        key,
                        explicit_approval=True,
                        manual_paper_trade=True,
                        costs=COSTS,
                        _signal_preflight=locked_verify,
                        _signal_postwrite=audit,
                    )
                else:
                    receipt = close_paper_spot(
                        session,
                        trade_id,
                        reference,
                        key,
                        explicit_approval=True,
                        costs=COSTS,
                        _signal_preflight=locked_verify,
                        _signal_postwrite=audit,
                    )
            return ManualPaperResult(receipt, reference, sample.received_at_ms)
        finally:
            lease.execute(text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": LOCK_KEY})
            lease.commit()
