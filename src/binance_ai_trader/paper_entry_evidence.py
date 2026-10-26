"""Derive immutable BT-027 pre-entry evidence from the PAPER DB and public feeds.

These receipts establish data integrity and freshness only. They do not approve
an AI model, guarantee a fill, or authorize a PAPER BUY.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from binance_ai_trader.market_data import MarketDataError
from binance_ai_trader.models import TradingSettingsRow
from binance_ai_trader.paper_execution import PaperExecutionRejected, _locked_state, _risk_state
from binance_ai_trader.public_spot_coherence import (
    CoherenceStatus,
    review_public_spot_coherence,
)
from binance_ai_trader.public_spot_cost_review import (
    PublicBookCostSample,
    PublicSpotCostClient,
)
from binance_ai_trader.public_spot_depth_review import PublicSpotDepth, PublicSpotDepthClient
from binance_ai_trader.public_spot_observation import (
    PublicSpotObservation,
    PublicSpotObservationClient,
)

BPS = Decimal("10000")
FEE_ASSUMPTION_BPS = Decimal("10")
FEE_ASSUMPTION_VERSION = "tradepilot_paper_fee_assumption_v1"
SLIPPAGE_ASSUMPTION_BPS = Decimal("5")
SLIPPAGE_ASSUMPTION_VERSION = "tradepilot_paper_slippage_assumption_v1"
MAX_EVIDENCE_AGE_MS = 3_000


class EntryEvidenceRejected(ValueError):
    """A PAPER snapshot or pre-entry market evidence failed closed."""


@dataclass(frozen=True, slots=True)
class PaperAccountEvidence:
    account_id: int
    settings_version: int
    journal_head_id: int
    journal_count: int
    snapshot_sha256: str
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PreEntryCostEvidence:
    evidence_sha256: str
    observed_at_ms: int
    reference_price: Decimal
    bid_price: Decimal
    ask_price: Decimal
    spread_bps: Decimal
    fee_cost_usdt: Decimal
    slippage_cost_usdt: Decimal
    spread_cost_usdt: Decimal
    payload: dict[str, Any]


def _decimal(value: Decimal) -> str:
    return "0" if value == 0 else format(value.normalize(), "f")


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def paper_account_evidence_in_session(
    session: Session, *, account_id: int, expected_settings_version: int
) -> PaperAccountEvidence:
    """Lock, reconcile and hash one consistent ledger/settings snapshot."""
    if account_id != 1:
        raise EntryEvidenceRejected("unsupported_paper_account")
    try:
        settings, account, positions, journal = _locked_state(session)
        settings_row = session.scalar(
            select(TradingSettingsRow).where(TradingSettingsRow.id == 1).with_for_update()
        )
        if settings_row is None or settings_row.version != expected_settings_version:
            raise EntryEvidenceRejected("stale_paper_settings_version")
        if account.settings_version > settings_row.version:
            raise EntryEvidenceRejected("paper_account_settings_mismatch")
        # No reliable mark source is persisted for open positions. Do not treat
        # entry cost as current market value when computing available equity.
        if any(position.status == "open" for position in positions):
            raise EntryEvidenceRejected("open_position_mark_unavailable")
        risk = _risk_state(account, journal)
    except PaperExecutionRejected as exc:
        raise EntryEvidenceRejected("paper_ledger_reconciliation_failed") from exc

    position_payload = [
        {
            "id": row.id,
            "trade_id": row.trade_id,
            "idempotency_key": row.idempotency_key,
            "symbol": row.symbol,
            "status": row.status,
            "quantity": _decimal(row.quantity),
            "entry_fill_price": _decimal(row.entry_fill_price),
            "entry_notional_usdt": _decimal(row.entry_notional_usdt),
            "entry_fee_usdt": _decimal(row.entry_fee_usdt),
            "stop_price": _decimal(row.stop_price),
            "estimated_stop_loss_usdt": _decimal(row.estimated_stop_loss_usdt),
            "exit_fill_price": None
            if row.exit_fill_price is None
            else _decimal(row.exit_fill_price),
            "exit_fee_usdt": None if row.exit_fee_usdt is None else _decimal(row.exit_fee_usdt),
            "net_proceeds_usdt": None
            if row.net_proceeds_usdt is None
            else _decimal(row.net_proceeds_usdt),
            "realized_pnl_usdt": None
            if row.realized_pnl_usdt is None
            else _decimal(row.realized_pnl_usdt),
            "opened_at": row.opened_at.isoformat(),
            "closed_at": None if row.closed_at is None else row.closed_at.isoformat(),
        }
        for row in positions
    ]
    journal_payload = [
        {
            "id": row.id,
            "position_id": row.position_id,
            "event_id": row.event_id,
            "idempotency_key": row.idempotency_key,
            "event_type": row.event_type,
            "cash_delta_usdt": _decimal(row.cash_delta_usdt),
            "balance_after_usdt": _decimal(row.balance_after_usdt),
            "fee_usdt": _decimal(row.fee_usdt),
            "realized_pnl_delta_usdt": _decimal(row.realized_pnl_delta_usdt),
            "quantity_delta": _decimal(row.quantity_delta),
            "created_at": row.created_at.isoformat(),
        }
        for row in journal
    ]
    if not journal:
        raise EntryEvidenceRejected("paper_journal_missing")
    consecutive_losses = 0
    for entry in reversed(journal):
        if entry.event_type != "spot_sell":
            continue
        if entry.realized_pnl_delta_usdt < 0:
            consecutive_losses += 1
        else:
            break
    payload: dict[str, Any] = {
        "schema_version": "paper-account-evidence-v1",
        "account_id": account.id,
        "mode": account.mode,
        "market_type": account.market_type,
        "settings_version": settings_row.version,
        "settings_spot_budget_usdt": _decimal(Decimal(settings.spot_capital_usdt)),
        "account_settings_version": account.settings_version,
        "initial_capital_usdt": _decimal(account.initial_capital_usdt),
        "cash_usdt": _decimal(account.cash_usdt),
        "realized_net_pnl_usdt": _decimal(account.realized_pnl_usdt),
        "paid_commissions_usdt": _decimal(account.paid_fees_usdt),
        "risk_equity_usdt": _decimal(risk.equity_usdt),
        "day_start_equity_usdt": _decimal(risk.day_start_equity_usdt),
        "peak_equity_usdt": _decimal(risk.peak_equity_usdt),
        "open_capital_usdt": _decimal(risk.reserved_capital_usdt),
        "open_positions": risk.open_positions,
        "consecutive_losses": consecutive_losses,
        "positions": position_payload,
        "journal": journal_payload,
        "journal_head_id": journal[-1].id,
        "journal_count": len(journal),
    }
    return PaperAccountEvidence(
        account.id,
        settings_row.version,
        journal[-1].id,
        len(journal),
        _digest(payload),
        payload,
    )


def capture_paper_account_evidence(
    engine: Engine, *, account_id: int, expected_settings_version: int
) -> PaperAccountEvidence:
    """Read a repeatable, row-locked PostgreSQL snapshot without mutating rows."""
    if engine.dialect.name != "postgresql":
        raise EntryEvidenceRejected("postgresql_required")
    isolated = engine.execution_options(isolation_level="REPEATABLE READ")
    with Session(isolated) as session, session.begin():
        return paper_account_evidence_in_session(
            session, account_id=account_id, expected_settings_version=expected_settings_version
        )


def collect_pre_entry_cost_evidence(
    intent: Any,
    *,
    candle_client: PublicSpotObservationClient,
    book_client: PublicSpotCostClient,
    depth_client: PublicSpotDepthClient,
) -> PreEntryCostEvidence:
    """Acquire and validate closed candles, BBO and depth from public REST clients."""
    if (
        type(candle_client) is not PublicSpotObservationClient
        or type(book_client) is not PublicSpotCostClient
        or type(depth_client) is not PublicSpotDepthClient
    ):
        raise EntryEvidenceRejected("server_owned_public_observation_clients_required")
    try:
        observation: PublicSpotObservation = candle_client.observe(
            symbol=intent.symbol, interval="1m", limit=100
        )
        book: PublicBookCostSample = book_client.observe(symbol=intent.symbol)
        depth: PublicSpotDepth = depth_client.observe(symbol=intent.symbol)
        now_ms = depth_client.now_ms()
        coherence = review_public_spot_coherence(
            observation,
            book,
            depth,
            expected_symbol=intent.symbol,
            now_ms=now_ms,
        )
    except (MarketDataError, ValueError, TypeError, AttributeError) as exc:
        raise EntryEvidenceRejected("pre_entry_market_observation_unavailable") from exc
    if coherence.status is not CoherenceStatus.COHERENT_RESEARCH_ONLY:
        raise EntryEvidenceRejected(
            "pre_entry_market_observation_" + coherence.status.value.lower()
        )
    if not 0 <= now_ms - coherence.locally_received_at_ms <= MAX_EVIDENCE_AGE_MS:  # type: ignore[operator]
        raise EntryEvidenceRejected("stale_pre_entry_market_evidence")
    spread = book.spread_bps
    notional = intent.order_notional_usdt
    fee_cost = notional * FEE_ASSUMPTION_BPS / BPS
    spread_cost = notional * spread / BPS
    slippage_cost = notional * SLIPPAGE_ASSUMPTION_BPS / BPS
    if (
        observation.reference_price != intent.reference_price
        or fee_cost != intent.fee_cost_usdt
        or spread_cost != intent.spread_cost_usdt
        or slippage_cost != intent.slippage_cost_usdt
        or intent.quantity > min(book.bid_quantity, book.ask_quantity)
    ):
        raise EntryEvidenceRejected("intent_costs_or_price_do_not_match_public_evidence")
    payload: dict[str, Any] = {
        "schema_version": "paper-pre-entry-cost-v1",
        "symbol": intent.symbol,
        "interval": "1m",
        "source": "binance_public_spot_coherence",
        "coherence_status": coherence.status.value,
        "signal_close_time_ms": observation.signal_close_time_ms,
        "price_requested_at_ms": observation.requested_at_ms,
        "price_received_at_ms": observation.received_at_ms,
        "reference_price": _decimal(observation.reference_price),
        "book_source": book.source,
        "book_requested_at_ms": book.requested_at_ms,
        "book_received_at_ms": book.received_at_ms,
        "bid_price": _decimal(book.bid),
        "ask_price": _decimal(book.ask),
        "bid_quantity": _decimal(book.bid_quantity),
        "ask_quantity": _decimal(book.ask_quantity),
        "book_update_id": depth.update_id,
        "depth_requested_at_ms": depth.requested_at_ms,
        "depth_received_at_ms": depth.received_at_ms,
        "depth_bids": [[_decimal(level.price), _decimal(level.quantity)] for level in depth.bids],
        "depth_asks": [[_decimal(level.price), _decimal(level.quantity)] for level in depth.asks],
        "spread_bps": _decimal(spread),
        "fee_assumption_bps": _decimal(FEE_ASSUMPTION_BPS),
        "fee_assumption_version": FEE_ASSUMPTION_VERSION,
        "slippage_assumption_bps": _decimal(SLIPPAGE_ASSUMPTION_BPS),
        "slippage_assumption_version": SLIPPAGE_ASSUMPTION_VERSION,
        "order_notional_usdt": _decimal(notional),
        "fee_cost_usdt": _decimal(fee_cost),
        "spread_cost_usdt": _decimal(spread_cost),
        "slippage_cost_usdt": _decimal(slippage_cost),
        # This is a caller/model estimate, recorded for integrity only; no
        # independent edge source is present in this stage.
        "expected_net_advantage_usdt": _decimal(intent.expected_net_advantage_usdt),
        "expected_net_advantage_verified": False,
        "trade_authorized": False,
    }
    return PreEntryCostEvidence(
        _digest(payload),
        coherence.locally_received_at_ms,  # type: ignore[arg-type]
        observation.reference_price,
        book.bid,
        book.ask,
        spread,
        fee_cost,
        slippage_cost,
        spread_cost,
        payload,
    )
