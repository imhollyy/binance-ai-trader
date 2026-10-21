"""Scheduler never starts implicitly, never busy-polls, stops gracefully."""

from __future__ import annotations

from threading import Event

import pytest
from sqlalchemy import create_engine

from binance_ai_trader.paper_scheduler import (
    SchedulerStatus,
    _run_loop,
    main,
    next_scheduler_wakeup_ms,
    run_paper_scheduler,
)
from binance_ai_trader.paper_worker import PaperWorkerReport, PaperWorkerStatus


def test_default_off_no_db_or_network_and_cli_refuses_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        result = run_paper_scheduler(engine)
        assert result.status is SchedulerStatus.DISABLED
        assert result.ticks == 0
        assert run_paper_scheduler(engine, enabled=True).status is SchedulerStatus.FAILED
        with pytest.raises(ValueError, match="invalid_scheduler_opt_in"):
            run_paper_scheduler(engine, enabled=1)  # type: ignore[arg-type]
    finally:
        engine.dispose()
    monkeypatch.delenv("PAPER_SCHEDULER_OPT_IN", raising=False)
    monkeypatch.setenv("TRADING_MODE", "paper")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://foo:secret@localhost/test")
    assert main([]) == 2
    assert main(["--enable-paper-scheduler"]) == 2
    monkeypatch.setenv("PAPER_SCHEDULER_OPT_IN", "not_explicit")
    assert main(["--enable-paper-scheduler"]) == 2
    monkeypatch.setenv("PAPER_SCHEDULER_OPT_IN", "I_UNDERSTAND_PAPER_ONLY")
    monkeypatch.setenv("TRADING_MODE", "live")
    assert main(["--enable-paper-scheduler"]) == 2


@pytest.mark.parametrize("interval,step", [("1m", 60_000), ("5m", 300_000), ("15m", 900_000)])
def test_wakeup_does_not_poll_twice_before_next_closed_candle(interval: str, step: int) -> None:
    now = 1_800_000_000_100
    new = next_scheduler_wakeup_ms(checked_at_ms=now, interval=interval, next_allowed_at_ms=None)
    assert new >= now + 60_000
    assert new >= (now // step + 1) * step + 1
    throttled = next_scheduler_wakeup_ms(
        checked_at_ms=now, interval=interval, next_allowed_at_ms=now + 600_000
    )
    assert throttled >= now + 600_000


def test_retry_after_wait_stop_and_two_ticks_with_virtual_clock() -> None:
    clock = [1_800_000_000_100]
    outputs: list[PaperWorkerReport] = []
    waits: list[float] = []
    stop = Event()

    def wait(seconds: float) -> bool:
        assert seconds >= 60
        waits.append(seconds)
        clock[0] += round(seconds * 1000)
        return False

    responses = [
        PaperWorkerReport(
            PaperWorkerStatus.RATE_LIMITED, "public_http_429", clock[0], clock[0] + 240_000
        ),
        PaperWorkerReport(PaperWorkerStatus.NO_SIGNAL, "no_signal", clock[0] + 240_000),
    ]

    def tick() -> PaperWorkerReport:
        return responses.pop(0)

    summary = _run_loop(
        interval="1m",
        clock_ms=lambda: clock[0],
        stop_event=stop,
        wait=wait,
        single_tick=tick,
        max_ticks=2,
        report=outputs.append,
    )
    assert summary.status is SchedulerStatus.LIMIT_REACHED
    assert summary.ticks == 2
    assert len(waits) == 1 and waits[0] >= 240
    assert outputs[0].status is PaperWorkerStatus.RATE_LIMITED
    assert summary.last_worker_status is PaperWorkerStatus.NO_SIGNAL


def test_sigterm_like_stop_and_disabled_tick_terminate_without_polling() -> None:
    now = 1_800_000_000_100
    stop = Event()
    seen: list[float] = []

    def wait(seconds: float) -> bool:
        seen.append(seconds)
        stop.set()
        return True

    summary = _run_loop(
        interval="1m",
        clock_ms=lambda: now,
        stop_event=stop,
        wait=wait,
        single_tick=lambda: PaperWorkerReport(PaperWorkerStatus.NO_SIGNAL, "none", now),
        max_ticks=None,
        report=None,
    )
    assert summary.status is SchedulerStatus.STOPPED and summary.ticks == 1
    assert len(seen) == 1
    disabled = _run_loop(
        interval="1m",
        clock_ms=lambda: now,
        stop_event=Event(),
        wait=lambda _: False,
        single_tick=lambda: PaperWorkerReport(PaperWorkerStatus.DISABLED, "off", now),
        max_ticks=None,
        report=None,
    )
    assert disabled.status is SchedulerStatus.DISABLED and disabled.ticks == 1


def test_worker_error_and_clock_regression_stop_fail_closed() -> None:
    now = [1_800_000_000_100]
    error = _run_loop(
        interval="1m",
        clock_ms=lambda: now[0],
        stop_event=Event(),
        wait=lambda _: False,
        single_tick=lambda: PaperWorkerReport(PaperWorkerStatus.ERROR, "error", now[0]),
        max_ticks=None,
        report=None,
    )
    assert error.status is SchedulerStatus.FAILED

    def backward() -> PaperWorkerReport:
        now[0] -= 1000
        return PaperWorkerReport(PaperWorkerStatus.READY, "cooldown_active", now[0])

    regression = _run_loop(
        interval="1m",
        clock_ms=lambda: now[0] + 2000,
        stop_event=Event(),
        wait=lambda _: False,
        single_tick=backward,
        max_ticks=None,
        report=None,
    )
    assert regression.status is SchedulerStatus.FAILED


@pytest.mark.parametrize(
    "kwargs",
    [
        {"checked_at_ms": -1, "interval": "1m", "next_allowed_at_ms": None},
        {"checked_at_ms": 10_000, "interval": "1s", "next_allowed_at_ms": None},
        {"checked_at_ms": 10_000, "interval": "1m", "next_allowed_at_ms": -2},
    ],
)
def test_wakeup_invalid_params_fail_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        next_scheduler_wakeup_ms(**kwargs)  # type: ignore[arg-type]
