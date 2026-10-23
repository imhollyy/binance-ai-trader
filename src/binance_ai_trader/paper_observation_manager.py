"""Disposable-only process manager and heartbeat-only PAPER observer child.

The child has no market-data, strategy, ledger, RiskGuard or exchange imports.
It only attests its OS identity, refreshes the fenced lease, and acknowledges STOP.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from uuid import uuid4

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from binance_ai_trader.paper_supervisor import (
    SupervisorRejected,
    SupervisorStatus,
    acknowledge_observer_stop,
    attest_observer_started,
    heartbeat_observer,
    mark_observer_failed,
    observer_start_enabled,
    prepare_observer_generation,
    read_worker_status,
    request_worker_stop,
)
from binance_ai_trader.process_identity import process_start_token

HEARTBEAT_SECONDS = 0.5
START_TIMEOUT_SECONDS = 8.0
STOP_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class ObserverHandle:
    worker_id: str
    generation: int
    manager_id: str


class PaperObservationProcessManager:
    """Starts only an isolated, observation-only OS child process."""

    def __init__(self, engine: Engine) -> None:
        if not observer_start_enabled(engine):
            raise SupervisorRejected("observation_start_not_enabled")
        self.engine = engine
        self.manager_id = str(uuid4())
        self.manager_pid = os.getpid()
        token = process_start_token(self.manager_pid)
        if token is None:
            raise SupervisorRejected("manager_process_identity_unavailable")
        self.manager_start_token = token
        self._process: subprocess.Popen[bytes] | None = None
        self._handle: ObserverHandle | None = None
        self._actor_token: str | None = None

    def start(
        self, *, expected_revision: int, idempotency_key: str, actor_token: str
    ) -> SupervisorStatus:
        self.status()
        prepared = prepare_observer_generation(
            self.engine,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            actor_token=actor_token,
            manager_id=self.manager_id,
            manager_pid=self.manager_pid,
            manager_start_token=self.manager_start_token,
        )
        if prepared.replay:
            current = self.status()
            if (
                current.worker_id == prepared.worker_id
                and current.generation == prepared.generation
                and current.manager_id == prepared.manager_id
            ):
                return current
            raise SupervisorRejected("start_replay_requires_reconciliation")

        child_url = make_url(self.engine.url).render_as_string(hide_password=False)
        child_env = {
            **os.environ,
            "DATABASE_URL": child_url,
            "PAPER_OBSERVER_CHILD": "1",
            "PAPER_OBSERVER_WORKER_ID": prepared.worker_id,
            "PAPER_OBSERVER_GENERATION": str(prepared.generation),
            "PAPER_OBSERVER_MANAGER_ID": self.manager_id,
            "PAPER_OBSERVER_MANAGER_PID": str(self.manager_pid),
            "PAPER_OBSERVER_MANAGER_TOKEN": self.manager_start_token,
        }
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [sys.executable, "-m", "binance_ai_trader.paper_observation_manager", "--child"],
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
        self._handle = ObserverHandle(prepared.worker_id, prepared.generation, self.manager_id)
        self._actor_token = actor_token
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                self.reconcile_child_exit()
                raise SupervisorRejected("observer_child_failed_to_start")
            current = self.status()
            if current.verified_running and current.generation == prepared.generation:
                return current
            time.sleep(0.05)
        self.stop(
            expected_revision=self.status().revision,
            expected_generation=prepared.generation,
            idempotency_key=f"start_timeout_{uuid4().hex}",
            actor_token=actor_token,
        )
        raise SupervisorRejected("observer_start_timeout")

    def status(self) -> SupervisorStatus:
        if (
            self._process is not None
            and self._process.poll() is not None
            and self._handle is not None
        ):
            with suppress(SupervisorRejected):
                mark_observer_failed(
                    self.engine,
                    worker_id=self._handle.worker_id,
                    generation=self._handle.generation,
                    reason="process_exited",
                )
        status = read_worker_status(self.engine)
        return replace(status, start_supported=True)

    def stop(
        self,
        *,
        expected_revision: int,
        expected_generation: int,
        idempotency_key: str,
        actor_token: str,
    ) -> SupervisorStatus:
        request_worker_stop(
            self.engine,
            expected_revision=expected_revision,
            expected_generation=expected_generation,
            idempotency_key=idempotency_key,
            actor_token=actor_token,
        )
        deadline = time.monotonic() + STOP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self.reconcile_child_exit()
            current = self.status()
            if current.state in {"STOPPED", "FAILED", "STALE"}:
                if self._process is not None and self._process.poll() is None:
                    try:
                        self._process.wait(timeout=max(0.0, deadline - time.monotonic()))
                    except subprocess.TimeoutExpired as exc:
                        raise SupervisorRejected("observer_stop_process_exit_unverified") from exc
                return current
            time.sleep(0.05)
        return self.status()

    def reconcile_child_exit(self) -> SupervisorStatus:
        return self.status()

    def shutdown(self) -> None:
        if self._handle is None or self._process is None or self._process.poll() is not None:
            return
        current = self.status()
        if current.generation != self._handle.generation or not current.verified_running:
            return
        try:
            self.stop(
                expected_revision=current.revision,
                expected_generation=current.generation,
                idempotency_key=f"shutdown_{uuid4().hex}",
                actor_token="local-observer-manager-shutdown",
            )
        except (SupervisorRejected, SQLAlchemyError):
            return


def _run_observer_child() -> int:
    if os.environ.get("PAPER_OBSERVER_CHILD") != "1":
        return 2
    raw_url = os.environ.get("DATABASE_URL")
    if not raw_url:
        return 2
    try:
        engine = create_engine(raw_url, pool_pre_ping=True, pool_timeout=2)
        worker_id = os.environ["PAPER_OBSERVER_WORKER_ID"]
        generation = int(os.environ["PAPER_OBSERVER_GENERATION"])
        manager_id = os.environ["PAPER_OBSERVER_MANAGER_ID"]
        manager_pid = int(os.environ["PAPER_OBSERVER_MANAGER_PID"])
        manager_token = os.environ["PAPER_OBSERVER_MANAGER_TOKEN"]
        own_pid = os.getpid()
        own_token = process_start_token(own_pid)
        if own_token is None:
            return 2
        attest_observer_started(
            engine,
            worker_id=worker_id,
            generation=generation,
            manager_id=manager_id,
            manager_pid=manager_pid,
            manager_start_token=manager_token,
            process_pid=own_pid,
            process_token=own_token,
        )
        while True:
            time.sleep(HEARTBEAT_SECONDS)
            if process_start_token(manager_pid) != manager_token:
                mark_observer_failed(
                    engine,
                    worker_id=worker_id,
                    generation=generation,
                    reason="manager_lost",
                    from_child=True,
                )
                return 1
            if not heartbeat_observer(engine, worker_id=worker_id, generation=generation):
                acknowledge_observer_stop(engine, worker_id=worker_id, generation=generation)
                return 0
    except (KeyError, ValueError, SupervisorRejected, SQLAlchemyError):
        return 2
    finally:
        if "engine" in locals():
            engine.dispose()


if __name__ == "__main__":
    raise SystemExit(_run_observer_child() if sys.argv[1:] == ["--child"] else 2)
