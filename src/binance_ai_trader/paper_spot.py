"""Immutable SPOT paper ledger. Simulation only: no exchange, DB or network I/O.

This is deliberately *not* a real order service. Only long Spot positions
are modeled; Futures funding/liquidation semantics are not approximated here.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from binance_ai_trader.paper_risk import (
    PaperRiskRequest,
    PaperRiskState,
    PaperRiskStatus,
    evaluate_paper_risk,
)
from binance_ai_trader.risk_schemas import RiskSettings

BPS = Decimal("10000")
HUNDRED = Decimal("100")
SUPPORTED = frozenset({"BTCUSDT", "ETHUSDT"})


class PaperSpotRejected(ValueError):
    """Simulation preflight or accounting constraint denied the request."""


@dataclass(frozen=True, slots=True)
class PaperCosts:
    """Explicit hypothetical taker fee and adverse execution price slippage."""

    fee_bps: int = 10
    slippage_bps: int = 5


@dataclass(frozen=True, slots=True)
class SpotBuyIntent:
    trade_id: str
    symbol: str
    reference_price: Decimal
    quantity: Decimal
    stop_price: Decimal


@dataclass(frozen=True, slots=True)
class PaperSpotPosition:
    trade_id: str
    symbol: str
    quantity: Decimal
    entry_fill_price: Decimal
    entry_notional_usdt: Decimal
    entry_fee_usdt: Decimal
    stop_price: Decimal
    estimated_stop_loss_usdt: Decimal


@dataclass(frozen=True, slots=True)
class PaperSpotExit:
    trade_id: str
    symbol: str
    exit_fill_price: Decimal
    exit_fee_usdt: Decimal
    net_proceeds_usdt: Decimal
    realized_pnl_usdt: Decimal


@dataclass(frozen=True, slots=True)
class PaperSpotBook:
    """Pure USDT paper ledger, never a Binance balance."""

    initial_capital_usdt: Decimal
    cash_usdt: Decimal
    positions: tuple[PaperSpotPosition, ...] = ()
    closed: tuple[PaperSpotExit, ...] = ()
    realized_pnl_usdt: Decimal = Decimal("0")
    paid_fees_usdt: Decimal = Decimal("0")


def new_paper_spot_book(settings: RiskSettings) -> PaperSpotBook:
    if settings.mode != "paper" or settings.market_type != "spot":
        raise PaperSpotRejected("spot_paper_only")
    capital = Decimal(settings.spot_capital_usdt)
    if capital <= 0:
        raise PaperSpotRejected("paper_spot_capital_missing")
    return PaperSpotBook(initial_capital_usdt=capital, cash_usdt=capital)


def _positive(value: Decimal) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


def _costs(costs: PaperCosts) -> tuple[Decimal, Decimal]:
    if (
        type(costs.fee_bps) is not int
        or type(costs.slippage_bps) is not int
        or not 0 <= costs.fee_bps <= 100
        or not 0 <= costs.slippage_bps <= 100
    ):
        raise PaperSpotRejected("invalid_simulation_costs")
    return Decimal(costs.fee_bps) / BPS, Decimal(costs.slippage_bps) / BPS


def _validate_book(book: PaperSpotBook, settings: RiskSettings) -> None:
    if (
        not _positive(book.initial_capital_usdt)
        or book.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
        or not isinstance(book.cash_usdt, Decimal)
        or not book.cash_usdt.is_finite()
        or book.cash_usdt < 0
        or not isinstance(book.paid_fees_usdt, Decimal)
        or not book.paid_fees_usdt.is_finite()
        or book.paid_fees_usdt < 0
    ):
        raise PaperSpotRejected("invalid_or_stale_spot_book")
    known_ids = [position.trade_id for position in book.positions]
    known_ids.extend(exit_event.trade_id for exit_event in book.closed)
    if len(known_ids) != len(set(known_ids)):
        raise PaperSpotRejected("duplicate_paper_trade_id")


def simulate_spot_buy(
    book: PaperSpotBook,
    settings: RiskSettings,
    risk_state: PaperRiskState,
    intent: SpotBuyIntent,
    *,
    explicit_approval: bool = False,
    manual_paper_trade: bool = False,
    costs: PaperCosts | None = None,
) -> PaperSpotBook:
    """Return a NEW immutable paper book or reject without any partial mutation."""

    if settings.mode != "paper" or settings.market_type != "spot":
        raise PaperSpotRejected("spot_paper_only")
    if manual_paper_trade and (
        type(manual_paper_trade) is not bool
        or explicit_approval is not True
        or settings.bot_enabled
        or not settings.manual_approval
    ):
        raise PaperSpotRejected("manual_paper_approval_required")
    _validate_book(book, settings)
    if (
        not isinstance(intent.trade_id, str)
        or not 1 <= len(intent.trade_id) <= 64
        or not intent.trade_id.isascii()
        or not all(ch.isalnum() or ch in "-_" for ch in intent.trade_id)
    ):
        raise PaperSpotRejected("invalid_trade_id")
    if intent.trade_id in {item.trade_id for item in book.positions} | {
        item.trade_id for item in book.closed
    }:
        raise PaperSpotRejected("duplicate_paper_trade_id")
    if intent.symbol not in SUPPORTED or intent.symbol not in settings.allowed_symbols:
        raise PaperSpotRejected("paper_symbol_not_allowed")
    if not all(_positive(v) for v in (intent.reference_price, intent.quantity, intent.stop_price)):
        raise PaperSpotRejected("invalid_price_or_quantity")
    if intent.stop_price >= intent.reference_price:
        raise PaperSpotRejected("stop_not_below_entry")
    fee_rate, slip_rate = _costs(costs if costs is not None else PaperCosts())

    # Prevent trusting a stale open-position/reservation snapshot.
    reserved = sum((position.entry_notional_usdt for position in book.positions), Decimal(0))
    if (
        risk_state.open_positions != len(book.positions)
        or risk_state.reserved_capital_usdt != reserved
        or (not book.positions and risk_state.equity_usdt != book.cash_usdt)
    ):
        raise PaperSpotRejected("risk_snapshot_mismatch")

    entry_fill = intent.reference_price * (1 + slip_rate)
    entry_notional = intent.quantity * entry_fill
    entry_fee = entry_notional * fee_rate
    spent = entry_notional + entry_fee
    stop_exit_fill = intent.stop_price * (1 - slip_rate)
    expected_exit_fee = intent.quantity * stop_exit_fill * fee_rate
    estimated_stop_loss = spent - intent.quantity * stop_exit_fill + expected_exit_fee
    stop_distance_pct = (entry_fill - intent.stop_price) * HUNDRED / entry_fill
    decision = evaluate_paper_risk(
        settings,
        PaperRiskRequest(
            proposed_notional_usdt=entry_notional,
            stop_distance_pct=stop_distance_pct,
        ),
        risk_state,
        manual_paper_trade=manual_paper_trade,
    )
    if decision.status == PaperRiskStatus.REJECTED:
        raise PaperSpotRejected(decision.reason)
    if decision.status == PaperRiskStatus.APPROVAL_REQUIRED and not explicit_approval:
        raise PaperSpotRejected("manual_approval_required")
    if spent > book.cash_usdt:
        raise PaperSpotRejected("insufficient_paper_spot_cash")
    if estimated_stop_loss > (
        book.initial_capital_usdt * Decimal(str(settings.risk_per_trade_pct)) / HUNDRED
    ):
        raise PaperSpotRejected("fee_slippage_adjusted_risk_limit")

    position = PaperSpotPosition(
        trade_id=intent.trade_id,
        symbol=intent.symbol,
        quantity=intent.quantity,
        entry_fill_price=entry_fill,
        entry_notional_usdt=entry_notional,
        entry_fee_usdt=entry_fee,
        stop_price=intent.stop_price,
        estimated_stop_loss_usdt=estimated_stop_loss,
    )
    return replace(
        book,
        cash_usdt=book.cash_usdt - spent,
        positions=(*book.positions, position),
        paid_fees_usdt=book.paid_fees_usdt + entry_fee,
    )


def simulate_spot_sell(
    book: PaperSpotBook,
    trade_id: str,
    reference_price: Decimal,
    *,
    costs: PaperCosts | None = None,
) -> PaperSpotBook:
    """Close an entire existing PAPER Spot long. No automatic stops or orders."""

    if not _positive(reference_price):
        raise PaperSpotRejected("invalid_exit_reference_price")
    fee_rate, slip_rate = _costs(costs if costs is not None else PaperCosts())
    matching = [position for position in book.positions if position.trade_id == trade_id]
    if len(matching) != 1:
        raise PaperSpotRejected("paper_position_not_open")
    position = matching[0]
    exit_fill = reference_price * (1 - slip_rate)
    gross = position.quantity * exit_fill
    exit_fee = gross * fee_rate
    proceeds = gross - exit_fee
    pnl = proceeds - position.entry_notional_usdt - position.entry_fee_usdt
    closed = PaperSpotExit(
        trade_id=trade_id,
        symbol=position.symbol,
        exit_fill_price=exit_fill,
        exit_fee_usdt=exit_fee,
        net_proceeds_usdt=proceeds,
        realized_pnl_usdt=pnl,
    )
    return replace(
        book,
        cash_usdt=book.cash_usdt + proceeds,
        positions=tuple(p for p in book.positions if p.trade_id != trade_id),
        closed=(*book.closed, closed),
        realized_pnl_usdt=book.realized_pnl_usdt + pnl,
        paid_fees_usdt=book.paid_fees_usdt + exit_fee,
    )
