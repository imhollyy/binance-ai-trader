"""Explicit-opt-in, default-OFF PAPER Spot scheduler.

No exchange order endpoints, no public HTTP routes, no implicit daemon startup.
The existing one-shot worker owns DB advisory locking, durable Retry-After,
quote verification, and atomic simulated journal entries.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from threading import Event

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS
from binance_ai_trader.paper_worker import (
    PaperWorkerReport,
    PaperWorkerStatus,
    run_paper_worker_once,
)

MIN_POLL_MS = 60_000
OPT_IN_VALUE = "I_UNDERSTAND_PAPER_ONLY"


class SchedulerStatus(StrEnum):
    DISABLED = "disabled"
    STOPPED = "stopped"
    FAILED = "failed"
    LIMIT_REACHED = "limit_reached"


@dataclass(frozen=True, slots=True)
class SchedulerSummary:
    status: SchedulerStatus
    reason: str
    ticks: int
    last_worker_status: PaperWorkerStatus | None = None
    next_wakeup_ms: int | None = None


def next_scheduler_wakeup_ms(
    *,
    checked_at_ms: int,
    interval: str,
    next_allowed_at_ms: int | None,
) -> int:
    """Never poll twice in the same closed-candle interval or inside cooldown."""

    if type(checked_at_ms) is not int or checked_at_ms <= 0 or interval not in INTERVAL_MS:
        raise ValueError("invalid_scheduler_clock_or_interval")
    if next_allowed_at_ms is not None and (
        type(next_allowed_at_ms) is not int or next_allowed_at_ms < 0
    ):
        raise ValueError("invalid_scheduler_cooldown")
    step = INTERVAL_MS[interval]
    next_candle_boundary_ms = (checked_at_ms // step + 1) * step + 1
    return max(
        checked_at_ms + MIN_POLL_MS,
        next_candle_boundary_ms,
        next_allowed_at_ms or 0,
    )


def _run_loop(
    *,
    interval: str,
    clock_ms: Callable[[], int],
    stop_event: Event,
    wait: Callable[[float], bool],
    single_tick: Callable[[], PaperWorkerReport],
    max_ticks: int | None,
    report: Callable[[PaperWorkerReport], None] | None,
) -> SchedulerSummary:
    ticks = 0
    last_status: PaperWorkerStatus | None = None
    while not stop_event.is_set():
        before = clock_ms()
        if type(before) is not int or before <= 0:
            return SchedulerSummary(SchedulerStatus.FAILED, "invalid_utc_clock", ticks)
        try:
            outcome = single_tick()
        except Exception:
            return SchedulerSummary(SchedulerStatus.FAILED, "paper_tick_failed", ticks)
        ticks += 1
        last_status = outcome.status
        if report is not None:
            # Host callback receives a bounded safe reason and PAPER journal key.
            report(outcome)
        if outcome.status is PaperWorkerStatus.DISABLED:
            return SchedulerSummary(
                SchedulerStatus.DISABLED, outcome.reason, ticks, outcome.status
            )
        if outcome.status is PaperWorkerStatus.ERROR:
            return SchedulerSummary(
                SchedulerStatus.FAILED, outcome.reason, ticks, outcome.status
            )
        if stop_event.is_set():
            return SchedulerSummary(
                SchedulerStatus.STOPPED, "stop_requested", ticks, outcome.status
            )
        current = clock_ms()
        if type(current) is not int or current < before:
            return SchedulerSummary(
                SchedulerStatus.FAILED, "utc_clock_regressed", ticks, outcome.status
            )
        target = next_scheduler_wakeup_ms(
            checked_at_ms=current,
            interval=interval,
            next_allowed_at_ms=outcome.next_allowed_at_ms,
        )
        if max_ticks is not None and ticks >= max_ticks:
            return SchedulerSummary(
                SchedulerStatus.LIMIT_REACHED,
                "configured_tick_limit",
                ticks,
                last_status,
                target,
            )
        # Event.wait wakes immediately on SIGINT/SIGTERM; no busy-poll or
        # trading during a cooldown. Re-check after spurious wakes.
        while not stop_event.is_set():
            observed = clock_ms()
            if type(observed) is not int or observed < current:
                return SchedulerSummary(
                    SchedulerStatus.FAILED, "utc_clock_regressed", ticks, last_status
                )
            if observed >= target:
                break
            if wait((target - observed) / 1000):
                break
    return SchedulerSummary(SchedulerStatus.STOPPED, "stop_requested", ticks, last_status)


def run_paper_scheduler(
    engine: Engine,
    *,
    enabled: bool = False,
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    stop_event: Event | None = None,
    max_ticks: int | None = None,
    clock_ms: Callable[[], int] | None = None,
    wait: Callable[[float], bool] | None = None,
    report: Callable[[PaperWorkerReport], None] | None = None,
) -> SchedulerSummary:
    """Run only on two explicit opt-ins: THIS flag + persisted PAPER settings.

    No scheduler is started by importing this module or by a database migration.
    The caller is responsible for deliberately invoking it in a local container.
    """

    if type(enabled) is not bool:
        raise ValueError("invalid_scheduler_opt_in")
    if not enabled:
        return SchedulerSummary(SchedulerStatus.DISABLED, "explicit_opt_in_required", 0)
    if (
        engine.dialect.name != "postgresql"
        or symbol not in SUPPORTED_SYMBOLS
        or interval not in INTERVAL_MS
        or (max_ticks is not None and (type(max_ticks) is not int or not 1 <= max_ticks <= 1000))
    ):
        return SchedulerSummary(SchedulerStatus.FAILED, "invalid_scheduler_config", 0)
    stop = stop_event or Event()
    clock = clock_ms or (lambda: time.time_ns() // 1_000_000)
    return _run_loop(
        interval=interval,
        clock_ms=clock,
        stop_event=stop,
        wait=wait or stop.wait,
        single_tick=lambda: run_paper_worker_once(
            engine,
            enabled=True,
            symbol=symbol,
            interval=interval,
            clock_ms=clock,
        ),
        max_ticks=max_ticks,
        report=report,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LOCAL PAPER-ONLY scheduler; default OFF")
    parser.add_argument("--enable-paper-scheduler", action="store_true")
    parser.add_argument("--symbol", choices=sorted(SUPPORTED_SYMBOLS), default="BTCUSDT")
    parser.add_argument("--interval", choices=sorted(INTERVAL_MS), default="1m")
    args = parser.parse_args(argv)

    # Refuse to construct an engine or load credentials unless BOTH explicit
    # command-line and local environment safety gates are in place.
    if not args.enable_paper_scheduler or os.environ.get("PAPER_SCHEDULER_OPT_IN") != OPT_IN_VALUE:
        print("PAPER scheduler disabled: explicit local opt-in required")
        return 2
    if os.environ.get("TRADING_MODE") != "paper":
        print("PAPER scheduler disabled: TRADING_MODE must be paper")
        return 2
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url.startswith(("postgresql+psycopg://", "postgresql://")):
        print("PAPER scheduler disabled: local PostgreSQL configuration required")
        return 2

    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    engine = create_engine(database_url, pool_pre_ping=True, pool_timeout=5)
    try:
        def log_safe(result: PaperWorkerReport) -> None:
            # Never log connection strings, credentials, prices, position cash,
            # arbitrary exception messages or identifiable user fields.
            print(
                f"PAPER worker: status={result.status.value} "
                f"reason={result.reason} checked_at_ms={result.checked_at_ms}",
                flush=True,
            )

        summary = run_paper_scheduler(
            engine,
            enabled=True,
            symbol=args.symbol,
            interval=args.interval,
            stop_event=stop,
            report=log_safe,
        )
        print(f"PAPER scheduler ended: status={summary.status.value} ticks={summary.ticks}")
        return 0 if summary.status in (SchedulerStatus.STOPPED, SchedulerStatus.DISABLED) else 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
