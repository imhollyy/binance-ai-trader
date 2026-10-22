"""Disposable PostgreSQL contract for verified worker lease and safe STOP."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import DBAPIError

from binance_ai_trader.api import create_app
from binance_ai_trader.api_config import ApiConfig
from binance_ai_trader.paper_supervisor import (
    SupervisorRejected,
    claim_worker,
    heartbeat_worker,
    read_worker_status,
    release_worker,
)

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


def test_worker_lease_restart_race_authorized_stop_and_append_only_audit() -> None:
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
        assert status.verified_running and not status.stale
        assert status.worker_id == worker_id and status.start_supported is False

        app = create_app(config=ApiConfig(database_url=None, admin_token=token), engine=engine)
        with TestClient(app) as client:
            assert client.get("/api/v1/paper/worker").status_code == 401
            got = client.get("/api/v1/paper/worker", headers={"X-Admin-Token": token})
            assert got.status_code == 200 and got.json()["verified_running"] is True
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
            assert stopped_request.json()["state"] == "STOP_REQUESTED"
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
