"""Internal transaction-safe PAPER Spot simulation; NEVER an exchange order service.

No HTTP routes, account credentials, scheduler or Binance order access exist here.
A single open position at a time is deliberate: without trustworthy marked equity
the risk gate refuses any new exposure. Every mutation owns its DB transaction.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal, DecimalException, Inexact, localcontext

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from binance_ai_trader.models import (
    PaperSpotAccountRow,
    PaperSpotJournalRow,
    PaperSpotPositionRow,
    TradingSettingsRow,
)
from binance_ai_trader.paper_risk import PaperRiskState
from binance_ai_trader.paper_spot import (
    PaperCosts,
    PaperSpotBook,
    PaperSpotPosition,
    PaperSpotRejected,
    SpotBuyIntent,
    simulate_spot_buy,
    simulate_spot_sell,
)
from binance_ai_trader.risk_schemas import RiskSettings

SCALE = Decimal("0.000000000000000001")
LockedPaperState = tuple[
    RiskSettings, PaperSpotAccountRow, list[PaperSpotPositionRow], list[PaperSpotJournalRow]
]
SignalPreflight = Callable[
    [
        Session,
        RiskSettings,
        PaperSpotAccountRow,
        list[PaperSpotPositionRow],
        list[PaperSpotJournalRow],
    ],
    None,
]
SignalPostWrite = Callable[[Session, PaperSpotJournalRow], None]


class PaperExecutionRejected(ValueError):
    """Fail-closed paper ledger denial; no exchange operations are attempted."""


@dataclass(frozen=True, slots=True)
class PaperTradeReceipt:
    """Immutable journal receipt, stable even after later position closes."""

    event_id: str
    idempotency_key: str
    trade_id: str
    event_type: str
    cash_after_usdt: Decimal
    fee_usdt: Decimal
    realized_pnl_delta_usdt: Decimal


def _money(value: Decimal) -> Decimal:
    """No hidden PostgreSQL rounding, overflow, float conversion or NaN."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise PaperExecutionRejected("invalid_paper_decimal")
    try:
        with localcontext(Context(prec=64, traps=[Inexact])):
            exact = value.quantize(SCALE)
    except DecimalException as exc:
        raise PaperExecutionRejected("unsupported_paper_precision") from exc
    if exact and exact.adjusted() >= 20:
        raise PaperExecutionRejected("paper_numeric_overflow")
    return exact


def _key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value.isascii()
        or not all(ch.isalnum() or ch in "-_." for ch in value)
        or value == "initial-capital"
    ):
        raise PaperExecutionRejected("invalid_paper_idempotency_key")
    return value


def _event_id(operation: str, key: str, payload: dict[str, str | int]) -> str:
    # The idempotency key is UNIQUE in the DB; this digest detects payload conflicts
    # without storing a second mutable request body or changing the D1 schema.
    data = json.dumps(
        {"v": 1, "op": operation, "key": key, "request": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _number(value: Decimal) -> str:
    with localcontext(Context(prec=64)):
        return format(value.normalize(), "f")


def _receipt(row: PaperSpotJournalRow, trade_id: str) -> PaperTradeReceipt:
    return PaperTradeReceipt(
        event_id=row.event_id,
        idempotency_key=row.idempotency_key,
        trade_id=trade_id,
        event_type=row.event_type,
        cash_after_usdt=row.balance_after_usdt,
        fee_usdt=row.fee_usdt,
        realized_pnl_delta_usdt=row.realized_pnl_delta_usdt,
    )


def _locked_state(session: Session) -> LockedPaperState:
    if session.get_bind().dialect.name != "postgresql":
        raise PaperExecutionRejected("postgresql_required")
    settings_row = session.scalar(
        select(TradingSettingsRow).where(TradingSettingsRow.id == 1).with_for_update()
    )
    if settings_row is None:
        raise PaperExecutionRejected("persisted_paper_settings_required")
    try:
        settings = RiskSettings.model_validate(settings_row.data)
    except ValidationError as exc:
        raise PaperExecutionRejected("invalid_persisted_paper_settings") from exc
    if settings.mode != "paper" or settings.market_type != "spot":
        raise PaperExecutionRejected("spot_paper_only")
    account = session.scalar(
        select(PaperSpotAccountRow).where(PaperSpotAccountRow.id == 1).with_for_update()
    )
    if account is None:
        raise PaperExecutionRejected("paper_account_bootstrap_required")
    if (
        account.mode != "paper"
        or account.market_type != "spot"
        or account.initial_capital_usdt != Decimal(settings.spot_capital_usdt)
        or account.settings_version > settings_row.version
    ):
        raise PaperExecutionRejected("paper_account_settings_mismatch")
    positions = list(
        session.scalars(
            select(PaperSpotPositionRow)
            .where(PaperSpotPositionRow.account_id == 1)
            .order_by(PaperSpotPositionRow.id)
        )
    )
    events = list(
        session.scalars(
            select(PaperSpotJournalRow)
            .where(PaperSpotJournalRow.account_id == 1)
            .order_by(PaperSpotJournalRow.id)
        )
    )
    _reconcile(account, positions, events)
    return settings, account, positions, events


def _reconcile(
    account: PaperSpotAccountRow,
    positions: list[PaperSpotPositionRow],
    events: list[PaperSpotJournalRow],
) -> None:
    """Verify that account, position and immutable journal agree before any write."""
    if not events or events[0].event_type != "account_opened":
        raise PaperExecutionRejected("missing_paper_opening_event")
    by_id = {item.id: item for item in positions}
    buys: set[int] = set()
    sells: set[int] = set()
    cash = Decimal(0)
    fees = Decimal(0)
    pnl = Decimal(0)
    for index, event in enumerate(events):
        cash = _money(cash + event.cash_delta_usdt)
        fees = _money(fees + event.fee_usdt)
        pnl = _money(pnl + event.realized_pnl_delta_usdt)
        if cash != event.balance_after_usdt:
            raise PaperExecutionRejected("paper_journal_cash_mismatch")
        if event.event_type == "account_opened":
            if (
                index != 0
                or event.position_id is not None
                or event.cash_delta_usdt != account.initial_capital_usdt
                or event.fee_usdt != 0
                or event.realized_pnl_delta_usdt != 0
                or event.quantity_delta != 0
            ):
                raise PaperExecutionRejected("invalid_paper_opening_event")
            continue
        position = by_id.get(event.position_id) if event.position_id is not None else None
        if position is None:
            raise PaperExecutionRejected("paper_event_position_missing")
        if event.event_type == "spot_buy":
            if (
                position.id in buys
                or event.idempotency_key != position.idempotency_key
                or event.cash_delta_usdt
                != -(position.entry_notional_usdt + position.entry_fee_usdt)
                or event.fee_usdt != position.entry_fee_usdt
                or event.realized_pnl_delta_usdt != 0
                or event.quantity_delta != position.quantity
            ):
                raise PaperExecutionRejected("paper_buy_journal_mismatch")
            buys.add(position.id)
        elif event.event_type == "spot_sell":
            if (
                position.id in sells
                or position.status != "closed"
                or event.cash_delta_usdt != position.net_proceeds_usdt
                or event.fee_usdt != position.exit_fee_usdt
                or event.realized_pnl_delta_usdt != position.realized_pnl_usdt
                or event.quantity_delta != -position.quantity
            ):
                raise PaperExecutionRejected("paper_sell_journal_mismatch")
            sells.add(position.id)
        else:
            raise PaperExecutionRejected("invalid_paper_event")
    if (
        cash != account.cash_usdt
        or fees != account.paid_fees_usdt
        or pnl != account.realized_pnl_usdt
        or buys != set(by_id)
        or any((position.id in sells) != (position.status == "closed") for position in positions)
        or sum(position.status == "open" for position in positions) > 1
    ):
        raise PaperExecutionRejected("paper_ledger_reconciliation_failed")


def _existing(
    events: list[PaperSpotJournalRow], key: str, digest: str, trade_id: str, operation: str
) -> PaperTradeReceipt | None:
    matches = [event for event in events if event.idempotency_key == key]
    if not matches:
        return None
    if len(matches) != 1 or matches[0].event_id != digest or matches[0].event_type != operation:
        raise PaperExecutionRejected("paper_idempotency_conflict")
    return _receipt(matches[0], trade_id)


def _risk_state(account: PaperSpotAccountRow, events: list[PaperSpotJournalRow]) -> PaperRiskState:
    """Realized-only equity is safe exclusively while there are no open positions."""
    now = datetime.now(UTC)
    cumulative = Decimal(0)
    peak = account.initial_capital_usdt
    start = account.initial_capital_usdt
    for event in events:
        if event.event_type != "spot_sell":
            continue
        if event.created_at > now:
            raise PaperExecutionRejected("future_paper_journal_event")
        if event.created_at.astimezone(UTC).date() < now.date():
            start += event.realized_pnl_delta_usdt
        cumulative += event.realized_pnl_delta_usdt
        peak = max(peak, account.initial_capital_usdt + cumulative)
    if account.cash_usdt != account.initial_capital_usdt + cumulative:
        raise PaperExecutionRejected("paper_equity_unreconciled")
    return PaperRiskState(
        equity_usdt=account.cash_usdt,
        day_start_equity_usdt=start,
        peak_equity_usdt=peak,
        reserved_capital_usdt=Decimal(0),
        open_positions=0,
    )


def open_paper_spot(
    session: Session,
    intent: SpotBuyIntent,
    idempotency_key: str,
    *,
    explicit_approval: bool = False,
    manual_paper_trade: bool = False,
    costs: PaperCosts | None = None,
    _signal_preflight: SignalPreflight | None = None,
    _signal_postwrite: SignalPostWrite | None = None,
) -> PaperTradeReceipt:
    """Simulate one Spot buy, transactionally; no API/exchange/order interaction."""
    key = _key(idempotency_key)
    assumptions = costs if costs is not None else PaperCosts()
    with localcontext(Context(prec=64)):
        price = _money(intent.reference_price)
        quantity = _money(intent.quantity)
        stop = _money(intent.stop_price)
        payload: dict[str, str | int] = {
            "trade": intent.trade_id,
            "symbol": intent.symbol,
            "price": _number(price),
            "quantity": _number(quantity),
            "stop": _number(stop),
            "fee_bps": assumptions.fee_bps,
            "slippage_bps": assumptions.slippage_bps,
        }
        if assumptions.spread_bps:
            payload["spread_bps"] = assumptions.spread_bps
        digest = _event_id(
            "spot_buy",
            key,
            payload,
        )
        with session.begin():
            settings, account, positions, events = _locked_state(session)
            previous = _existing(events, key, digest, intent.trade_id, "spot_buy")
            if previous is not None:
                return previous
            if _signal_preflight is not None:
                _signal_preflight(session, settings, account, positions, events)
            if any(position.trade_id == intent.trade_id for position in positions):
                raise PaperExecutionRejected("duplicate_paper_trade_id")
            if any(position.status == "open" for position in positions):
                raise PaperExecutionRejected("unmarked_open_position_fail_closed")
            state = _risk_state(account, events)
            book = PaperSpotBook(
                initial_capital_usdt=account.initial_capital_usdt,
                cash_usdt=account.cash_usdt,
                realized_pnl_usdt=account.realized_pnl_usdt,
                paid_fees_usdt=account.paid_fees_usdt,
            )
            try:
                opened = simulate_spot_buy(
                    book,
                    settings,
                    state,
                    SpotBuyIntent(intent.trade_id, intent.symbol, price, quantity, stop),
                    explicit_approval=explicit_approval,
                    manual_paper_trade=manual_paper_trade,
                    costs=assumptions,
                )
            except PaperSpotRejected as exc:
                raise PaperExecutionRejected(str(exc)) from exc
            position = opened.positions[-1]
            record = PaperSpotPositionRow(
                account_id=1,
                trade_id=position.trade_id,
                idempotency_key=key,
                symbol=position.symbol,
                status="open",
                quantity=_money(position.quantity),
                entry_fill_price=_money(position.entry_fill_price),
                entry_notional_usdt=_money(position.entry_notional_usdt),
                entry_fee_usdt=_money(position.entry_fee_usdt),
                stop_price=_money(position.stop_price),
                estimated_stop_loss_usdt=_money(position.estimated_stop_loss_usdt),
            )
            cash_after = _money(opened.cash_usdt)
            fee = _money(position.entry_fee_usdt)
            total_fees = _money(opened.paid_fees_usdt)
            session.add(record)
            session.flush()
            journal = PaperSpotJournalRow(
                account_id=1,
                position_id=record.id,
                event_id=digest,
                idempotency_key=key,
                event_type="spot_buy",
                cash_delta_usdt=_money(cash_after - account.cash_usdt),
                balance_after_usdt=cash_after,
                fee_usdt=fee,
                quantity_delta=_money(position.quantity),
            )
            session.add(journal)
            account.cash_usdt = cash_after
            account.paid_fees_usdt = total_fees
            session.flush()
            if _signal_postwrite is not None:
                _signal_postwrite(session, journal)
                session.flush()
            return _receipt(journal, position.trade_id)


def close_paper_spot(
    session: Session,
    trade_id: str,
    reference_price: Decimal,
    idempotency_key: str,
    *,
    explicit_approval: bool = False,
    costs: PaperCosts | None = None,
    _signal_preflight: SignalPreflight | None = None,
    _signal_postwrite: SignalPostWrite | None = None,
) -> PaperTradeReceipt:
    """Simulate full closure even if bot is disabled; never send an order."""
    key = _key(idempotency_key)
    assumptions = costs if costs is not None else PaperCosts()
    with localcontext(Context(prec=64)):
        price = _money(reference_price)
        payload: dict[str, str | int] = {
            "trade": trade_id,
            "price": _number(price),
            "fee_bps": assumptions.fee_bps,
            "slippage_bps": assumptions.slippage_bps,
        }
        if assumptions.spread_bps:
            payload["spread_bps"] = assumptions.spread_bps
        digest = _event_id(
            "spot_sell",
            key,
            payload,
        )
        with session.begin():
            settings, account, positions, events = _locked_state(session)
            previous = _existing(events, key, digest, trade_id, "spot_sell")
            if previous is not None:
                return previous
            if _signal_preflight is not None:
                _signal_preflight(session, settings, account, positions, events)
            if settings.manual_approval and not explicit_approval:
                raise PaperExecutionRejected("manual_approval_required")
            matched = [position for position in positions if position.trade_id == trade_id]
            if len(matched) != 1 or matched[0].status != "open":
                raise PaperExecutionRejected("paper_position_not_open")
            stored = matched[0]
            if sum(position.status == "open" for position in positions) != 1:
                raise PaperExecutionRejected("paper_position_state_uncertain")
            book = PaperSpotBook(
                initial_capital_usdt=account.initial_capital_usdt,
                cash_usdt=account.cash_usdt,
                positions=(
                    PaperSpotPosition(
                        trade_id=stored.trade_id,
                        symbol=stored.symbol,
                        quantity=stored.quantity,
                        entry_fill_price=stored.entry_fill_price,
                        entry_notional_usdt=stored.entry_notional_usdt,
                        entry_fee_usdt=stored.entry_fee_usdt,
                        stop_price=stored.stop_price,
                        estimated_stop_loss_usdt=stored.estimated_stop_loss_usdt,
                    ),
                ),
                realized_pnl_usdt=account.realized_pnl_usdt,
                paid_fees_usdt=account.paid_fees_usdt,
            )
            try:
                finished = simulate_spot_sell(book, trade_id, price, costs=assumptions)
            except PaperSpotRejected as exc:
                raise PaperExecutionRejected(str(exc)) from exc
            exit_event = finished.closed[-1]
            cash_after = _money(finished.cash_usdt)
            fee = _money(exit_event.exit_fee_usdt)
            pnl = _money(exit_event.realized_pnl_usdt)
            new_fees = _money(finished.paid_fees_usdt)
            new_pnl = _money(finished.realized_pnl_usdt)
            stored.status = "closed"
            stored.exit_fill_price = _money(exit_event.exit_fill_price)
            stored.exit_fee_usdt = fee
            stored.net_proceeds_usdt = _money(exit_event.net_proceeds_usdt)
            stored.realized_pnl_usdt = pnl
            stored.closed_at = datetime.now(UTC)
            journal = PaperSpotJournalRow(
                account_id=1,
                position_id=stored.id,
                event_id=digest,
                idempotency_key=key,
                event_type="spot_sell",
                cash_delta_usdt=_money(cash_after - account.cash_usdt),
                balance_after_usdt=cash_after,
                fee_usdt=fee,
                realized_pnl_delta_usdt=pnl,
                quantity_delta=_money(-stored.quantity),
            )
            session.add(journal)
            account.cash_usdt = cash_after
            account.paid_fees_usdt = new_fees
            account.realized_pnl_usdt = new_pnl
            session.flush()
            if _signal_postwrite is not None:
                _signal_postwrite(session, journal)
                session.flush()
            return _receipt(journal, trade_id)
