"""Default-OFF, explicitly invoked single-tick PAPER Spot worker (NO scheduler).

Only public Binance Spot REST reads + strictly bounded CLOSED candle persistence
and already-locked PAPER journal mutations. No authenticated trading or orders.
A PostgreSQL session advisory lock spans HTTP and atomic ledger verification;
a durable cooldown survives failure, crash and process restart.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

import httpx
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from binance_ai_trader.market_data import (
    INTERVAL_MS,
    SUPPORTED_SYMBOLS,
    MarketDataError,
    MarketDataInvalid,
    MarketRateLimited,
    PublicSpotMarketClient,
)
from binance_ai_trader.market_ingest import ingest_once
from binance_ai_trader.models import PaperSpotAccountRow, PaperWorkerGuardRow, TradingSettingsRow
from binance_ai_trader.paper_execution import PaperExecutionRejected
from binance_ai_trader.paper_observed_execution import (
    ObservedPaperStatus,
    run_observed_paper_spot_once,
)
from binance_ai_trader.paper_strategy import SmaCrossParameters
from binance_ai_trader.public_spot_observation import PublicSpotObservationClient
from binance_ai_trader.risk_schemas import RiskSettings

ADVISORY_LOCK_KEY = 7_812_243_019
BASE_BACKOFF_MS = 60_000
MAX_BACKOFF_MS = 3_600_000
DEFAULT_PARAMS = SmaCrossParameters()


class PaperWorkerStatus(StrEnum):
    DISABLED = "disabled"
    READY = "ready"
    NO_SIGNAL = "no_signal"
    PAPER_RECORDED = "paper_recorded"
    SOURCE_BLOCKED = "source_blocked"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PaperWorkerReport:
    status: PaperWorkerStatus
    reason: str
    checked_at_ms: int
    next_allowed_at_ms: int | None = None
    signal_id: str | None = None


def _result(
    status: PaperWorkerStatus,
    reason: str,
    checked_at_ms: int,
    *,
    until: int | None = None,
    key: str | None = None,
) -> PaperWorkerReport:
    return PaperWorkerReport(status, reason, checked_at_ms, until, key)


def _configured(engine: Engine, symbol: str) -> str | None:
    """The enable flag and account must be valid BEFORE public I/O or DB writes."""

    with Session(engine) as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        settings_row = session.get(TradingSettingsRow, 1)
        account = session.get(PaperSpotAccountRow, 1)
        if settings_row is None or account is None:
            return "paper_account_or_settings_missing"
        try:
            settings = RiskSettings.model_validate(settings_row.data)
        except ValidationError:
            return "invalid_paper_settings"
        if (
            settings.mode != "paper"
            or settings.market_type != "spot"
            or not settings.bot_enabled
            or settings.manual_approval
            or symbol not in settings.allowed_symbols
            or account.mode != "paper"
            or account.market_type != "spot"
            or account.settings_version != settings_row.version
            or account.initial_capital_usdt != settings.spot_capital_usdt
        ):
            return "paper_worker_not_authorized_by_settings"
    return None


def _store_guard(
    engine: Engine,
    *,
    attempt_ms: int,
    next_allowed_ms: int,
    failures: int,
    status: PaperWorkerStatus,
) -> None:
    """Write cooldown only, never balance or a journal entry."""

    with Session(engine) as session, session.begin():
        row = session.get(PaperWorkerGuardRow, 1)
        if row is None:
            session.add(
                PaperWorkerGuardRow(
                    id=1,
                    last_attempt_at_ms=attempt_ms,
                    next_allowed_at_ms=next_allowed_ms,
                    consecutive_failures=failures,
                    last_status=status.value,
                )
            )
        else:
            if attempt_ms < row.last_attempt_at_ms:
                raise ValueError("paper_worker_clock_moved_backward")
            row.last_attempt_at_ms = attempt_ms
            row.next_allowed_at_ms = max(row.next_allowed_at_ms, next_allowed_ms)
            row.consecutive_failures = failures
            row.last_status = status.value


def _cooldown(
    *,
    symbol: str,
    interval: str,
    attempts: int,
    rate_limit: MarketRateLimited | None,
) -> int:
    """Conservative bounded exponential backoff with deterministic jitter."""

    digest = hashlib.sha256(f"{symbol}:{interval}:{attempts}".encode("ascii")).digest()
    jitter = int.from_bytes(digest[:2], "big") % 15_001
    delay = min(MAX_BACKOFF_MS, BASE_BACKOFF_MS * 2 ** min(attempts, 6))
    if rate_limit is not None:
        if rate_limit.status_code == 418:
            delay = max(delay, 900_000)
        if rate_limit.retry_after_seconds is not None:
            delay = max(delay, min(MAX_BACKOFF_MS, rate_limit.retry_after_seconds * 1000))
    return min(MAX_BACKOFF_MS, max(delay + jitter, INTERVAL_MS[interval]))


def run_paper_worker_once(
    engine: Engine,
    *,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    enabled: bool = False,
    limit: int = 100,
    params: SmaCrossParameters = DEFAULT_PARAMS,
    transport: httpx.BaseTransport | None = None,
    clock_ms: Callable[[], int] | None = None,
) -> PaperWorkerReport:
    """One deliberate PAPER run. Never schedules/starts itself.

    Must have BOTH enabled=True here and persisted bot_enabled=True with
    manual_approval=False. A process scheduler is NOT wired by this package.
    The optional transport/clock injection is ONLY for offline test fixtures.
    """

    now = clock_ms or (lambda: time.time_ns() // 1_000_000)
    stamp = now()
    if type(stamp) is not int or stamp <= 0:
        raise ValueError("invalid_worker_clock")
    if type(enabled) is not bool:
        raise ValueError("invalid_worker_enabled")
    if not enabled:
        return _result(PaperWorkerStatus.DISABLED, "explicit_worker_opt_in_required", stamp)
    if engine.dialect.name != "postgresql":
        return _result(PaperWorkerStatus.ERROR, "postgresql_required", stamp)
    if (
        symbol not in SUPPORTED_SYMBOLS
        or interval not in INTERVAL_MS
        or type(limit) is not int
        or not 50 <= limit <= 1000
        or not isinstance(params, SmaCrossParameters)
    ):
        return _result(PaperWorkerStatus.ERROR, "invalid_worker_configuration", stamp)

    # Session-scoped PostgreSQL lock, held on this pinned CONNECTION while
    # other independent sessions do bounded HTTP and PAPER transactions.
    with engine.connect() as lease:
        acquired = bool(
            lease.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": ADVISORY_LOCK_KEY},
            ).scalar_one()
        )
        lease.commit()
        if not acquired:
            return _result(PaperWorkerStatus.READY, "another_worker_holds_lock", stamp)
        try:
            try:
                denied = _configured(engine, symbol)
                if denied is not None:
                    return _result(PaperWorkerStatus.DISABLED, denied, stamp)
                with Session(engine) as session:
                    guard = session.get(PaperWorkerGuardRow, 1)
                    until = guard.next_allowed_at_ms if guard else 0
                    failures = guard.consecutive_failures if guard else 0
                    last = guard.last_attempt_at_ms if guard else 0
                if stamp < last:
                    return _result(PaperWorkerStatus.ERROR, "utc_clock_regressed", stamp)
                if stamp < until:
                    return _result(PaperWorkerStatus.READY, "cooldown_active", stamp, until=until)

                # Claim a future cooldown BEFORE I/O. A crash/restart cannot
                # trigger aggressive public polling or a duplicate tick.
                minimum = max(INTERVAL_MS[interval], BASE_BACKOFF_MS)
                _store_guard(
                    engine, attempt_ms=stamp, next_allowed_ms=stamp + minimum,
                    failures=failures, status=PaperWorkerStatus.READY,
                )
                try:
                    market = PublicSpotMarketClient(transport=transport, clock_ms=now)
                    ingest = ingest_once(
                        engine, market, symbol=symbol, interval=interval, limit=limit,
                        now_ms=now(), require_ready=True,
                    )
                    if ingest.missing_open_times_ms:
                        raise MarketDataInvalid("stored_spot_candle_gap")
                    observer = PublicSpotObservationClient(transport=transport, clock_ms=now)
                    executed = run_observed_paper_spot_once(
                        engine, observer, symbol=symbol, interval=interval,
                        params=params, limit=limit,
                    )
                except (MarketDataError, PaperExecutionRejected, ValueError) as exc:
                    source = exc if isinstance(exc, MarketDataError) else exc.__cause__
                    restricted = source if isinstance(source, MarketRateLimited) else None
                    attempts = min(failures + 1, 10)
                    status = (
                        PaperWorkerStatus.RATE_LIMITED if restricted
                        else PaperWorkerStatus.SOURCE_BLOCKED
                    )
                    delay = _cooldown(
                        symbol=symbol, interval=interval, attempts=attempts,
                        rate_limit=restricted,
                    )
                    _store_guard(
                        engine, attempt_ms=stamp, next_allowed_ms=stamp + delay,
                        failures=attempts, status=status,
                    )
                    reason = (
                        f"public_http_{restricted.status_code}"
                        if restricted else "source_or_paper_preflight_rejected"
                    )
                    return _result(status, reason, stamp, until=stamp + delay)
                status = (
                    PaperWorkerStatus.PAPER_RECORDED
                    if executed.status is ObservedPaperStatus.RECORDED
                    else PaperWorkerStatus.NO_SIGNAL
                )
                _store_guard(
                    engine, attempt_ms=stamp, next_allowed_ms=stamp + minimum,
                    failures=0, status=status,
                )
                return _result(
                    status, status.value, stamp, until=stamp + minimum,
                    key=executed.receipt.idempotency_key if executed.receipt else None,
                )
            except Exception:
                # No arbitrary exception messages, tokens or network URLs in
                # public status. Error is a fail-closed terminal one-shot result.
                return _result(PaperWorkerStatus.ERROR, "paper_worker_internal_error", stamp)
        finally:
            lease.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": ADVISORY_LOCK_KEY},
            )
            lease.commit()
