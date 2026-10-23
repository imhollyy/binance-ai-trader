"""Disposable PostgreSQL contract for verified worker lease and safe STOP."""

from __future__ import annotations

import os
import select
import socket
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Barrier, Event, Lock, Thread
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.paper_observation_manager import PaperObservationProcessManager
from binance_ai_trader.paper_supervisor import (
    SupervisorRejected,
    claim_worker,
    heartbeat_observer,
    heartbeat_worker,
    read_worker_status,
    release_worker,
)
from binance_ai_trader.process_identity import process_start_token

URL = os.environ.get("TEST_POSTGRES_URL", "")
ISOLATED = bool(
    URL
    and make_url(URL).get_backend_name() == "postgresql"
    and make_url(URL).host in ("localhost", "127.0.0.1")
    and make_url(URL).database == "trader_worker_supervisor_test"
    and os.environ.get("PAPER_SUPERVISOR_TEST_ISOLATED") == "1"
)
pytestmark = pytest.mark.skipif(
    not ISOLATED, reason="Dedicated disposable trader_worker_supervisor_test only"
)


class _TcpDropProxy:
    """Local PostgreSQL protocol relay that can drop all worker connections."""

    def __init__(self, host: str, port: int) -> None:
        self.target = (host, port)
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.listener.settimeout(0.1)
        self.port = int(self.listener.getsockname()[1])
        self.stopping = Event()
        self.lock = Lock()
        self.blocked = False
        self.sockets: set[socket.socket] = set()
        self.accept_thread = Thread(target=self._accept, daemon=True)
        self.accept_thread.start()

    def block(self) -> None:
        with self.lock:
            self.blocked = True
            sockets = tuple(self.sockets)
        for connection in sockets:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()

    def unblock(self) -> None:
        with self.lock:
            self.blocked = False

    def close(self) -> None:
        self.stopping.set()
        self.block()
        self.listener.close()
        self.accept_thread.join(timeout=2)

    def _accept(self) -> None:
        while not self.stopping.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self.lock:
                blocked = self.blocked
            if blocked:
                client.close()
                continue
            try:
                upstream = socket.create_connection(self.target, timeout=2)
            except OSError:
                client.close()
                continue
            with self.lock:
                self.sockets.update((client, upstream))
            Thread(target=self._relay, args=(client, upstream), daemon=True).start()

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        try:
            while not self.stopping.is_set():
                readable, _, _ = select.select((client, upstream), (), (), 0.1)
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    (upstream if source is client else client).sendall(payload)
        except OSError:
            return
        finally:
            with self.lock:
                self.sockets.discard(client)
                self.sockets.discard(upstream)
            client.close()
            upstream.close()


def test_worker_lease_restart_race_authorized_stop_and_append_only_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert URL
    engine: Engine = create_engine(URL, pool_pre_ping=True)
    token = "w" * 40
    try:
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT count(*) FROM paper_worker_runtime")).scalar_one()
                == 0
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM paper_worker_supervisor_events")
                ).scalar_one()
                == 0
            )
        barrier = Barrier(2)

        def claim() -> tuple[str, int | None]:
            worker_id = str(uuid4())
            barrier.wait()
            try:
                return worker_id, claim_worker(engine, worker_id)
            except SupervisorRejected:
                return worker_id, None

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = list(pool.map(lambda _index: claim(), range(2)))
        winners = [(worker, revision) for worker, revision in claims if revision is not None]
        assert len(winners) == 1
        worker_id, revision = winners[0]
        assert revision == 1
        status = read_worker_status(engine)
        assert not status.verified_running and status.stale
        assert status.worker_id == worker_id and status.start_supported is False

        monkeypatch.delenv("PAPER_OBSERVER_START_TEST_ISOLATED", raising=False)
        monkeypatch.delenv("PAPER_SUPERVISOR_TEST_ISOLATED", raising=False)
        app = create_app(config=ApiConfig(database_url=None, admin_token=token), engine=engine)
        with TestClient(app) as client:
            assert client.get("/api/v1/paper/worker").status_code == 401
            got = client.get("/api/v1/paper/worker", headers={"X-Admin-Token": token})
            assert got.status_code == 200 and got.json()["verified_running"] is False
            assert got.json()["state"] == "STALE"
            assert client.post("/api/v1/paper/worker/start").status_code == 404
            stale = client.post(
                "/api/v1/paper/worker/stop",
                headers={"X-Admin-Token": token, "Idempotency-Key": "stop-stale-0001"},
                json={"expected_revision": revision + 1},
            )
            assert stale.status_code == 409
            stop_headers = {"X-Admin-Token": token, "Idempotency-Key": "stop-command-0001"}
            stopped_request = client.post(
                "/api/v1/paper/worker/stop",
                headers=stop_headers,
                json={"expected_revision": revision},
            )
            assert stopped_request.status_code == 200
            assert stopped_request.json()["state"] == "STALE"
            assert stopped_request.json()["verified_running"] is False
            assert (
                client.post(
                    "/api/v1/paper/worker/stop",
                    headers=stop_headers,
                    json={"expected_revision": revision},
                ).status_code
                == 200
            )
        assert heartbeat_worker(engine, worker_id) is False
        release_worker(engine, worker_id)
        assert read_worker_status(engine).state == "STOPPED"

        restarted_worker = str(uuid4())
        claim_worker(engine, restarted_worker)
        with engine.begin() as connection:
            connection.execute(text("UPDATE paper_worker_runtime SET lease_until_ms=0 WHERE id=1"))
        stale_status = read_worker_status(engine)
        assert stale_status.state == "STALE" and not stale_status.verified_running
        with pytest.raises(SupervisorRejected, match="worker_lease_expired"):
            heartbeat_worker(engine, restarted_worker)
        replacement_worker = str(uuid4())
        claim_worker(engine, replacement_worker)
        assert read_worker_status(engine).worker_id == replacement_worker
        release_worker(engine, replacement_worker)

        with engine.connect() as connection:
            events: int = connection.execute(
                text("SELECT count(*) FROM paper_worker_supervisor_events")
            ).scalar_one()
            assert events >= 6
            actor: str = connection.execute(
                text(
                    "SELECT actor_sha256 FROM paper_worker_supervisor_events "
                    "WHERE idempotency_key='stop-command-0001'"
                )
            ).scalar_one()
            assert actor != token and len(actor) == 64
        for statement in (
            "UPDATE paper_worker_supervisor_events SET revision=revision",
            "DELETE FROM paper_worker_supervisor_events",
            "TRUNCATE paper_worker_supervisor_events",
        ):
            with pytest.raises(DBAPIError), engine.begin() as connection:
                connection.execute(text(statement))
    finally:
        engine.dispose()


def test_process_managed_observation_start_stop_and_fencing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert URL
    monkeypatch.setenv("DATABASE_URL", URL)
    monkeypatch.setenv("PAPER_SUPERVISOR_TEST_ISOLATED", "1")
    monkeypatch.setenv("PAPER_OBSERVER_START_TEST_ISOLATED", "1")
    engine = create_engine(URL, pool_pre_ping=True)
    token = "observer-test-token-40-bytes-long!!"
    app = create_app(config=ApiConfig(database_url=None, admin_token=token), engine=engine)
    try:
        with engine.connect() as connection:
            ledger_before = int(
                connection.execute(text("SELECT count(*) FROM paper_spot_journal")).scalar_one()
            )
        baseline = read_worker_status(engine)
        assert baseline.state == "STOPPED" and baseline.generation == 0
        with TestClient(app) as client:
            denied = client.post(
                "/api/v1/paper/worker/start",
                headers={"Idempotency-Key": "observer_start_unauth"},
                json={"expected_revision": 0, "mode": "OBSERVATION_ONLY"},
            )
            assert denied.status_code == 401
            bad_origin = client.post(
                "/api/v1/paper/worker/start",
                headers={
                    "X-Admin-Token": token,
                    "Origin": "https://untrusted.invalid",
                    "Idempotency-Key": "observer_start_bad_origin",
                },
                json={"expected_revision": 0, "mode": "OBSERVATION_ONLY"},
            )
            assert bad_origin.status_code == 403
            initial = client.get("/api/v1/paper/worker", headers={"X-Admin-Token": token}).json()
            assert initial["start_supported"] is True
            started = client.post(
                "/api/v1/paper/worker/start",
                headers={"X-Admin-Token": token, "Idempotency-Key": "observer_start_0001"},
                json={"expected_revision": baseline.revision, "mode": "OBSERVATION_ONLY"},
            )
            assert started.status_code == 200, started.json()
            first = started.json()
            assert first["state"] == "RUNNING" and first["verified_running"] is True
            assert first["process_manager_verified"] is True
            assert first["observation_only"] is True and first["generation"] == 1
            assert first["process_pid"] > 0 and first["manager_id"]

            replay = client.post(
                "/api/v1/paper/worker/start",
                headers={"X-Admin-Token": token, "Idempotency-Key": "observer_start_0001"},
                json={"expected_revision": baseline.revision, "mode": "OBSERVATION_ONLY"},
            )
            assert replay.status_code == 200 and replay.json()["generation"] == 1
            assert replay.json()["worker_id"] == first["worker_id"]
            payload_conflict = client.post(
                "/api/v1/paper/worker/start",
                headers={"X-Admin-Token": token, "Idempotency-Key": "observer_start_0001"},
                json={
                    "expected_revision": baseline.revision + 1,
                    "mode": "OBSERVATION_ONLY",
                },
            )
            assert payload_conflict.status_code == 409

            duplicate = client.post(
                "/api/v1/paper/worker/start",
                headers={"X-Admin-Token": token, "Idempotency-Key": "observer_start_0002"},
                json={"expected_revision": replay.json()["revision"], "mode": "OBSERVATION_ONLY"},
            )
            assert duplicate.status_code == 409

            latest = client.get("/api/v1/paper/worker", headers={"X-Admin-Token": token}).json()
            stopped = client.post(
                "/api/v1/paper/worker/stop",
                headers={"X-Admin-Token": token, "Idempotency-Key": "observer_stop_0001"},
                json={
                    "expected_revision": latest["revision"],
                    "expected_generation": latest["generation"],
                },
            )
            assert stopped.status_code == 200, stopped.json()
            assert stopped.json()["state"] == "STOPPED"
            assert stopped.json()["verified_running"] is False

        with engine.connect() as connection:
            final = connection.execute(
                text("SELECT state,generation,execution_mode FROM paper_worker_runtime WHERE id=1")
            ).one()
            assert tuple(final) == ("STOPPED", 1, "OBSERVATION_ONLY")
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM paper_worker_supervisor_events "
                        "WHERE event_type='OBSERVER_STOP_ACKNOWLEDGED' AND worker_generation=1"
                    )
                ).scalar_one()
                == 1
            )
            assert (
                connection.execute(text("SELECT count(*) FROM paper_spot_journal")).scalar_one()
                == ledger_before
            )
            assert (
                connection.execute(
                    text("SELECT count(*) FROM paper_entry_ledger_commits")
                ).scalar_one()
                == 0
            )
        with engine.connect() as connection:
            child_identity = connection.execute(
                text("SELECT process_pid,process_start_token FROM paper_worker_runtime WHERE id=1")
            ).one()
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and process_start_token(child_identity.process_pid)
            == child_identity.process_start_token
        ):
            time.sleep(0.02)
        assert process_start_token(child_identity.process_pid) != child_identity.process_start_token
    finally:
        engine.dispose()

    # Exercise a real child crash and monotonic generation replacement. A late
    # heartbeat carrying the old worker identity must not affect the new child.
    engine = create_engine(URL, pool_pre_ping=True)
    manager = PaperObservationProcessManager(engine)
    try:
        before_restart = read_worker_status(engine)
        first = manager.start(
            expected_revision=before_restart.revision,
            idempotency_key="observer_restart_start_01",
            actor_token=token,
        )
        assert first.verified_running
        first_worker_id = first.worker_id
        first_generation = first.generation
        assert first_worker_id is not None
        assert manager._process is not None
        manager._process.kill()
        manager._process.wait(timeout=5)
        failed = manager.reconcile_child_exit()
        assert failed.state == "FAILED" and failed.generation == first_generation

        replacement = manager.start(
            expected_revision=failed.revision,
            idempotency_key="observer_restart_start_02",
            actor_token=token,
        )
        assert replacement.verified_running
        assert replacement.generation == first_generation + 1
        assert replacement.worker_id != first_worker_id
        with pytest.raises(SupervisorRejected, match="stale_worker_generation"):
            heartbeat_observer(engine, worker_id=first_worker_id, generation=first_generation)
        replacement = manager.status()
        control_barrier = Barrier(2)

        def compete_start() -> bool:
            control_barrier.wait()
            try:
                manager.start(
                    expected_revision=replacement.revision,
                    idempotency_key="observer_start_stop_race",
                    actor_token=token,
                )
                return True
            except SupervisorRejected:
                return False

        def compete_stop() -> str:
            control_barrier.wait()
            return manager.stop(
                expected_revision=replacement.revision,
                expected_generation=replacement.generation,
                idempotency_key="observer_restart_stop_02",
                actor_token=token,
            ).state

        with ThreadPoolExecutor(max_workers=2) as pool:
            start_result = pool.submit(compete_start)
            stop_result = pool.submit(compete_stop)
            assert start_result.result(timeout=10) is False
            assert stop_result.result(timeout=10) == "STOPPED"
        assert read_worker_status(engine).generation == replacement.generation
        assert manager._process is not None
        manager._process.wait(timeout=5)

        # Expired leases cannot be revived by delayed heartbeat writes. Once
        # that child exits, a strictly newer generation may be claimed.
        next_status = manager.start(
            expected_revision=read_worker_status(engine).revision,
            idempotency_key="observer_lease_expiry_start",
            actor_token=token,
        )
        with engine.begin() as connection:
            connection.execute(text("UPDATE paper_worker_runtime SET lease_until_ms=0 WHERE id=1"))
        assert read_worker_status(engine).state == "STALE"
        with pytest.raises(SupervisorRejected, match="observer_lease_lost"):
            heartbeat_observer(
                engine, worker_id=next_status.worker_id or "", generation=next_status.generation
            )
        assert manager._process is not None
        manager._process.wait(timeout=5)
        expired = manager.reconcile_child_exit()
        assert expired.state == "FAILED" and expired.generation == next_status.generation
        final_observer = manager.start(
            expected_revision=expired.revision,
            idempotency_key="observer_lease_expiry_restart",
            actor_token=token,
        )
        assert final_observer.generation == next_status.generation + 1
        stopped = manager.stop(
            expected_revision=final_observer.revision,
            expected_generation=final_observer.generation,
            idempotency_key="observer_lease_expiry_stop",
            actor_token=token,
        )
        assert stopped.state == "STOPPED"
    finally:
        manager.shutdown()
        engine.dispose()

    # Kill the manager process itself. Its child must detect the manager's
    # process-start token disappearing, persist MANAGER_LOST, and exit. A new
    # manager then starts only a higher generation.
    engine = create_engine(URL, pool_pre_ping=True)
    try:
        before_manager_restart = read_worker_status(engine)
        ready = tmp_path / "observer-manager.ready"
        manager_code = """
import os,time
from pathlib import Path
from sqlalchemy import create_engine
from binance_ai_trader.paper_observation_manager import PaperObservationProcessManager
from binance_ai_trader.paper_supervisor import read_worker_status
engine=create_engine(os.environ['DATABASE_URL'],pool_pre_ping=True)
manager=PaperObservationProcessManager(engine)
status=manager.start(expected_revision=int(os.environ['REV']),
    idempotency_key='observer_manager_crash_01',actor_token='manager-crash-test-token')
Path(os.environ['READY']).write_text(f'{status.generation}|{status.process_pid}|{status.worker_id}')
while True: time.sleep(1)
"""
        manager_process = subprocess.Popen(
            [sys.executable, "-c", manager_code],
            env={
                **os.environ,
                "DATABASE_URL": URL,
                "REV": str(before_manager_restart.revision),
                "READY": str(ready),
            },
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not ready.exists():
            time.sleep(0.02)
        assert ready.exists()
        manager_generation = int(ready.read_text(encoding="utf-8").split("|")[0])
        manager_process.kill()
        manager_process.wait(timeout=5)

        deadline = time.monotonic() + 12
        recovered = read_worker_status(engine)
        while time.monotonic() < deadline and recovered.state != "FAILED":
            time.sleep(0.1)
            recovered = read_worker_status(engine)
        assert recovered.state == "FAILED"
        assert recovered.generation == manager_generation
        # FAILED is persisted just before the observer process returns from
        # its manager-loss path. Do not try to replace that generation until
        # the OS confirms the old PID/start-token pair has exited; the
        # supervisor correctly fences a still-live child during this window.
        with engine.connect() as connection:
            orphan = connection.execute(
                text(
                    "SELECT process_pid,process_start_token FROM paper_worker_runtime "
                    "WHERE id=1 AND generation=:generation"
                ),
                {"generation": manager_generation},
            ).one()
        deadline = time.monotonic() + 5
        while (
            time.monotonic() < deadline
            and process_start_token(orphan.process_pid) == orphan.process_start_token
        ):
            time.sleep(0.02)
        assert process_start_token(orphan.process_pid) != orphan.process_start_token
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM paper_worker_supervisor_events "
                        "WHERE event_type='OBSERVER_MANAGER_LOST' AND worker_generation=:generation"
                    ),
                    {"generation": manager_generation},
                ).scalar_one()
                == 1
            )

        restarted_manager = PaperObservationProcessManager(engine)
        try:
            replayed_after_restart = restarted_manager.start(
                expected_revision=before_manager_restart.revision,
                idempotency_key="observer_manager_crash_01",
                actor_token="manager-crash-test-token",
            )
            assert replayed_after_restart.state == "FAILED"
            assert replayed_after_restart.generation == manager_generation
            restarted = restarted_manager.start(
                expected_revision=recovered.revision,
                idempotency_key="observer_manager_crash_02",
                actor_token=token,
            )
            assert restarted.verified_running
            assert restarted.generation == manager_generation + 1
            stopped = restarted_manager.stop(
                expected_revision=restarted.revision,
                expected_generation=restarted.generation,
                idempotency_key="observer_manager_crash_stop",
                actor_token=token,
            )
            assert stopped.state == "STOPPED"
            with pytest.raises(
                SupervisorRejected,
                match="legacy_worker_claim_cannot_replace_observer_generation",
            ):
                claim_worker(engine, str(uuid4()))
        finally:
            restarted_manager.shutdown()
    finally:
        if "manager_process" in locals() and manager_process.poll() is None:
            manager_process.kill()
            manager_process.wait(timeout=5)
        engine.dispose()

    # Drop live PostgreSQL protocol connections for an observation worker.
    # The worker must exit without claiming STOPPED; after connectivity returns,
    # the manager records failure and an idempotent retry cannot create a gen.
    engine = create_engine(URL, pool_pre_ping=True)
    parsed_url = make_url(URL)
    proxy = _TcpDropProxy(parsed_url.host or "127.0.0.1", parsed_url.port or 5432)
    proxy_url = parsed_url.set(host="127.0.0.1", port=proxy.port).render_as_string(
        hide_password=False
    )
    monkeypatch.setenv("DATABASE_URL", proxy_url)
    proxy_engine = create_engine(proxy_url, pool_pre_ping=True, pool_timeout=2)
    outage_manager = PaperObservationProcessManager(proxy_engine)
    try:
        before_outage = read_worker_status(engine)
        started = outage_manager.start(
            expected_revision=before_outage.revision,
            idempotency_key="observer_db_outage_start",
            actor_token=token,
        )
        assert started.verified_running
        assert outage_manager._process is not None
        proxy.block()
        with pytest.raises(SQLAlchemyError):
            outage_manager.status()
        outage_manager._process.wait(timeout=10)
        proxy.unblock()
        failed = outage_manager.reconcile_child_exit()
        assert failed.state == "FAILED" and failed.generation == started.generation
        replay = outage_manager.start(
            expected_revision=before_outage.revision,
            idempotency_key="observer_db_outage_start",
            actor_token=token,
        )
        assert replay.state == "FAILED" and replay.generation == started.generation
        restarted = outage_manager.start(
            expected_revision=failed.revision,
            idempotency_key="observer_db_outage_restart",
            actor_token=token,
        )
        assert restarted.generation == started.generation + 1
        stopped = outage_manager.stop(
            expected_revision=restarted.revision,
            expected_generation=restarted.generation,
            idempotency_key="observer_db_outage_stop",
            actor_token=token,
        )
        assert stopped.state == "STOPPED"
    finally:
        proxy.unblock()
        outage_manager.shutdown()
        proxy_engine.dispose()
        proxy.close()
        engine.dispose()
    monkeypatch.setenv("DATABASE_URL", URL)

    # Competing START claims from separate OS processes serialize on the
    # supervisor transaction lock: only one generation and one durable intent.
    engine = create_engine(URL, pool_pre_ping=True)
    try:
        status = read_worker_status(engine)
        ready_files = [tmp_path / f"observer-controller-{i}.ready" for i in range(2)]
        result = [tmp_path / f"observer-controller-{i}.result" for i in range(2)]
        go = tmp_path / "observer-controller.go"
        code = """
import os,time
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine
from binance_ai_trader.process_identity import process_start_token
from binance_ai_trader.paper_supervisor import prepare_observer_generation
engine=create_engine(os.environ['DATABASE_URL'],pool_pre_ping=True)
pid=os.getpid(); manager=str(uuid4()); token=process_start_token(pid)
Path(os.environ['READY']).write_text('ready')
deadline=time.monotonic()+20
while not Path(os.environ['GO']).exists() and time.monotonic()<deadline: time.sleep(.005)
try:
    value=prepare_observer_generation(engine,expected_revision=int(os.environ['REV']),
        idempotency_key=os.environ['IDEM'],actor_token='controller-test-token',
        manager_id=manager,manager_pid=pid,manager_start_token=token)
    Path(os.environ['RESULT']).write_text('OK|'+value.worker_id+'|'+str(value.generation))
except Exception as exc:
    Path(os.environ['RESULT']).write_text('NO|'+type(exc).__name__)
os._exit(0)
"""
        children: list[subprocess.Popen[bytes]] = []
        for index in range(2):
            child_env = {
                **os.environ,
                "DATABASE_URL": URL,
                "REV": str(status.revision),
                "IDEM": f"controller_race_{index:02d}",
                "READY": str(ready_files[index]),
                "RESULT": str(result[index]),
                "GO": str(go),
            }
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", code],
                    env=child_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(path.exists() for path in ready_files):
            time.sleep(0.02)
        assert all(path.exists() for path in ready_files)
        go.write_text("go", encoding="utf-8")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not all(path.exists() for path in result):
            time.sleep(0.02)
        assert all(path.exists() for path in result)
        outcomes = [path.read_text(encoding="utf-8") for path in result]
        assert sum(value.startswith("OK|") for value in outcomes) == 1, outcomes
        winner = next(value for value in outcomes if value.startswith("OK|")).split("|")
        generation = int(winner[2])
        assert generation == status.generation + 1
        orphaned = read_worker_status(engine)
        assert orphaned.state == "STALE" and orphaned.generation == generation
        recovery_manager = PaperObservationProcessManager(engine)
        try:
            recovered = recovery_manager.start(
                expected_revision=orphaned.revision,
                idempotency_key="controller_orphan_recovery",
                actor_token="controller-test-token",
            )
            assert recovered.generation == generation + 1 and recovered.verified_running
            stopped = recovery_manager.stop(
                expected_revision=recovered.revision,
                expected_generation=recovered.generation,
                idempotency_key="controller_orphan_recovery_stop",
                actor_token="controller-test-token",
            )
            assert stopped.state == "STOPPED"
        finally:
            recovery_manager.shutdown()
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text(
                        "SELECT count(*) FROM paper_worker_supervisor_events "
                        "WHERE event_type='OBSERVER_MANAGER_LOST' "
                        "AND worker_generation=:generation"
                    ),
                    {"generation": generation},
                ).scalar_one()
                == 1
            )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        engine.dispose()
