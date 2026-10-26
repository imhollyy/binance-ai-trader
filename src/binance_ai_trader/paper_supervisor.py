"""Database-backed worker identity and stop signalling; no start authorization."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from binance_ai_trader.process_identity import process_matches

LOCK_ID = 27027027
LEASE_MS = 120_000
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_IDEMPOTENCY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")


class SupervisorRejected(ValueError):
    """A worker lease or stop request was invalid or stale."""


@dataclass(frozen=True, slots=True)
class SupervisorStatus:
    state: str
    revision: int
    worker_id: str | None
    heartbeat_at_ms: int | None
    lease_until_ms: int | None
    verified_running: bool
    stale: bool
    start_supported: bool = False
    generation: int = 0
    manager_id: str | None = None
    process_pid: int | None = None
    process_manager_verified: bool = False
    observation_only: bool = False


@dataclass(frozen=True, slots=True)
class ObserverGeneration:
    worker_id: str
    generation: int
    revision: int
    manager_id: str
    replay: bool = False


def observer_start_enabled(engine: Engine) -> bool:
    """Allow process-managed START only for the exact local supervisor fixture."""
    raw_url = os.environ.get("DATABASE_URL")
    if not (
        raw_url
        and os.environ.get("PAPER_OBSERVER_START_TEST_ISOLATED") == "1"
        and os.environ.get("PAPER_SUPERVISOR_TEST_ISOLATED") == "1"
    ):
        return False
    try:
        from sqlalchemy.engine import make_url

        expected = make_url(raw_url)
    except (ValueError, TypeError):
        return False
    actual = engine.url
    return (
        actual == expected
        and actual.get_backend_name() == "postgresql"
        and actual.host in {"127.0.0.1", "localhost"}
        and actual.database == "trader_worker_supervisor_test"
    )


def prepare_observer_generation(
    engine: Engine,
    *,
    expected_revision: int,
    idempotency_key: str,
    actor_token: str,
    manager_id: str,
    manager_pid: int,
    manager_start_token: str,
) -> ObserverGeneration:
    """Persist a monotonically increasing observation-only fencing generation."""
    if not observer_start_enabled(engine):
        raise SupervisorRejected("observation_start_not_enabled")
    if (
        type(expected_revision) is not int
        or expected_revision < 0
        or _IDEMPOTENCY.fullmatch(idempotency_key) is None
        or not actor_token
        or _UUID.fullmatch(manager_id) is None
        or not process_matches(manager_pid, manager_start_token)
    ):
        raise SupervisorRejected("invalid_observation_start")
    actor = hashlib.sha256(actor_token.encode("utf-8")).hexdigest()
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        existing = (
            session.execute(
                text(
                    "SELECT revision,event_type,actor_sha256,payload,worker_id,worker_generation "
                    "FROM paper_worker_supervisor_events WHERE idempotency_key=:key"
                ),
                {"key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None:
            payload = existing["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if (
                existing["event_type"] != "OBSERVER_START_REQUESTED"
                or existing["actor_sha256"] != actor
                or payload.get("expected_revision") != expected_revision
            ):
                raise SupervisorRejected("idempotency_key_conflict")
            return ObserverGeneration(
                worker_id=str(existing["worker_id"]),
                generation=int(existing["worker_generation"]),
                revision=int(existing["revision"]),
                manager_id=str(payload["manager_id"]),
                replay=True,
            )
        now = _now_ms(session)
        session.execute(
            text(
                "INSERT INTO paper_worker_runtime(id,state,revision,heartbeat_at_ms,"
                "lease_until_ms,generation,execution_mode) "
                "VALUES(1,'STOPPED',0,0,0,0,'INERT') ON CONFLICT (id) DO NOTHING"
            )
        )
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,lease_until_ms,generation,execution_mode,"
                    "manager_pid,"
                    "manager_start_token,process_pid,process_start_token FROM paper_worker_runtime "
                    "WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one()
        )
        revision = int(row["revision"])
        if revision != expected_revision:
            raise SupervisorRejected("stale_worker_revision")
        active = (
            row["state"] in {"STARTING", "RUNNING", "STOP_REQUESTED"}
            and int(row["lease_until_ms"]) > now
        )
        manager_alive = process_matches(row["manager_pid"], row["manager_start_token"])
        process_alive = process_matches(row["process_pid"], row["process_start_token"])
        if active and (
            int(row["generation"]) == 0
            or row["execution_mode"] != "OBSERVATION_ONLY"
            or manager_alive
            or process_alive
        ):
            raise SupervisorRejected("observer_already_active")
        if process_alive:
            raise SupervisorRejected("observer_process_still_alive")
        generation = int(row["generation"]) + 1
        if active:
            # Both OS identities are dead despite an unexpired lease. Record
            # the orphaned generation before assigning its replacement.
            revision += 1
            session.execute(
                text(
                    "UPDATE paper_worker_runtime SET state='FAILED',revision=:revision,"
                    "lease_until_ms=0 WHERE id=1 AND generation=:generation"
                ),
                {"revision": revision, "generation": int(row["generation"])},
            )
            _append(
                session,
                event_id=f"observer_recovery_failure_{int(row['generation'])}",
                idempotency_key=None,
                revision=revision,
                event_type="OBSERVER_MANAGER_LOST",
                worker_id=str(row["worker_id"]),
                payload={"reason": "orphaned_processes"},
                worker_generation=int(row["generation"]),
            )
        revision += 1
        worker_id = str(uuid4())
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET worker_id=:worker,state='STARTING',"
                "revision=:revision,generation=:generation,manager_id=:manager,"
                "manager_pid=:manager_pid,manager_start_token=:manager_token,process_pid=NULL,"
                "process_start_token=NULL,execution_mode='OBSERVATION_ONLY',heartbeat_at_ms=:now,"
                "lease_until_ms=:lease WHERE id=1"
            ),
            {
                "worker": worker_id,
                "revision": revision,
                "generation": generation,
                "manager": manager_id,
                "manager_pid": manager_pid,
                "manager_token": manager_start_token,
                "now": now,
                "lease": now + LEASE_MS,
            },
        )
        _append(
            session,
            event_id=f"observer_start_{generation}_{hashlib.sha256(idempotency_key.encode()).hexdigest()}",
            idempotency_key=idempotency_key,
            revision=revision,
            event_type="OBSERVER_START_REQUESTED",
            worker_id=worker_id,
            actor_sha256=actor,
            payload={"expected_revision": expected_revision, "manager_id": manager_id},
            worker_generation=generation,
        )
        return ObserverGeneration(worker_id, generation, revision, manager_id)


def attest_observer_started(
    engine: Engine,
    *,
    worker_id: str,
    generation: int,
    manager_id: str,
    manager_pid: int,
    manager_start_token: str,
    process_pid: int,
    process_token: str,
) -> None:
    if (
        not process_matches(manager_pid, manager_start_token)
        or not process_matches(process_pid, process_token)
        or os.getpid() != process_pid
    ):
        raise SupervisorRejected("observer_process_attestation_failed")
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        row = (
            session.execute(
                text(
                    "SELECT revision,state,worker_id,generation,manager_id,manager_pid,"
                    "manager_start_token FROM paper_worker_runtime WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or (
            row["state"] != "STARTING"
            or row["worker_id"] != worker_id
            or int(row["generation"]) != generation
            or row["manager_id"] != manager_id
            or int(row["manager_pid"]) != manager_pid
            or row["manager_start_token"] != manager_start_token
        ):
            raise SupervisorRejected("stale_worker_generation")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET state='RUNNING',revision=:revision,"
                "process_pid=:pid,process_start_token=:token,heartbeat_at_ms=:now,"
                "lease_until_ms=:lease WHERE id=1 AND generation=:generation"
            ),
            {
                "revision": revision,
                "pid": process_pid,
                "token": process_token,
                "now": now,
                "lease": now + LEASE_MS,
                "generation": generation,
            },
        )
        _append(
            session,
            event_id=f"observer_started_{generation}",
            idempotency_key=None,
            revision=revision,
            event_type="OBSERVER_STARTED",
            worker_id=worker_id,
            payload={"process_pid": process_pid},
            worker_generation=generation,
        )


def heartbeat_observer(engine: Engine, *, worker_id: str, generation: int) -> bool:
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        row = (
            session.execute(text("SELECT * FROM paper_worker_runtime WHERE id=1 FOR UPDATE"))
            .mappings()
            .one_or_none()
        )
        if row is None or row["worker_id"] != worker_id or int(row["generation"]) != generation:
            raise SupervisorRejected("stale_worker_generation")
        if row["state"] == "STOP_REQUESTED":
            return False
        if (
            row["state"] != "RUNNING"
            or int(row["lease_until_ms"]) <= now
            or not process_matches(row["manager_pid"], row["manager_start_token"])
            or not process_matches(row["process_pid"], row["process_start_token"])
            or os.getpid() != int(row["process_pid"])
        ):
            raise SupervisorRejected("observer_lease_lost")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET revision=:revision,heartbeat_at_ms=:now,"
                "lease_until_ms=:lease WHERE id=1 AND worker_id=:worker AND generation=:generation"
            ),
            {
                "revision": revision,
                "now": now,
                "lease": now + LEASE_MS,
                "worker": worker_id,
                "generation": generation,
            },
        )
        _append(
            session,
            event_id=f"observer_heartbeat_{generation}_{revision}",
            idempotency_key=None,
            revision=revision,
            event_type="OBSERVER_HEARTBEAT",
            worker_id=worker_id,
            worker_generation=generation,
        )
        return True


def acknowledge_observer_stop(engine: Engine, *, worker_id: str, generation: int) -> None:
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        row = (
            session.execute(text("SELECT * FROM paper_worker_runtime WHERE id=1 FOR UPDATE"))
            .mappings()
            .one_or_none()
        )
        if row is None or row["worker_id"] != worker_id or int(row["generation"]) != generation:
            raise SupervisorRejected("stale_worker_generation")
        if (
            row["state"] != "STOP_REQUESTED"
            or not process_matches(row["process_pid"], row["process_start_token"])
            or os.getpid() != int(row["process_pid"])
        ):
            raise SupervisorRejected("observer_stop_acknowledgement_invalid")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET state='STOPPED',revision=:revision,"
                "heartbeat_at_ms=:now,lease_until_ms=0 WHERE id=1 AND generation=:generation"
            ),
            {"revision": revision, "now": now, "generation": generation},
        )
        _append(
            session,
            event_id=f"observer_stop_ack_{generation}",
            idempotency_key=None,
            revision=revision,
            event_type="OBSERVER_STOP_ACKNOWLEDGED",
            worker_id=worker_id,
            worker_generation=generation,
        )


def mark_observer_failed(
    engine: Engine, *, worker_id: str, generation: int, reason: str, from_child: bool = False
) -> None:
    if reason not in {"process_exited", "manager_lost", "database_unavailable"}:
        raise SupervisorRejected("invalid_observer_failure_reason")
    event_type = "OBSERVER_MANAGER_LOST" if reason == "manager_lost" else "OBSERVER_FAILED"
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        row = (
            session.execute(text("SELECT * FROM paper_worker_runtime WHERE id=1 FOR UPDATE"))
            .mappings()
            .one_or_none()
        )
        if row is None or row["worker_id"] != worker_id or int(row["generation"]) != generation:
            raise SupervisorRejected("stale_worker_generation")
        if row["state"] in {"STOPPED", "FAILED"}:
            return
        if from_child:
            if os.getpid() != row["process_pid"] or not process_matches(
                row["process_pid"], row["process_start_token"]
            ):
                raise SupervisorRejected("observer_process_attestation_failed")
        elif process_matches(row["process_pid"], row["process_start_token"]):
            raise SupervisorRejected("observer_process_still_alive")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET state='FAILED',revision=:revision,"
                "heartbeat_at_ms=:now,lease_until_ms=0 WHERE id=1 AND generation=:generation"
            ),
            {"revision": revision, "now": now, "generation": generation},
        )
        _append(
            session,
            event_id=f"observer_failure_{generation}",
            idempotency_key=None,
            revision=revision,
            event_type=event_type,
            worker_id=worker_id,
            payload={"reason": reason},
            worker_generation=generation,
        )


def _now_ms(session: Session) -> int:
    return int(
        session.execute(
            text("SELECT FLOOR(EXTRACT(EPOCH FROM clock_timestamp())*1000)::bigint")
        ).scalar_one()
    )


def _append(
    session: Session,
    *,
    event_id: str,
    idempotency_key: str | None,
    revision: int,
    event_type: str,
    worker_id: str | None,
    actor_sha256: str | None = None,
    payload: dict[str, object] | None = None,
    worker_generation: int | None = None,
) -> None:
    session.execute(
        text("""INSERT INTO paper_worker_supervisor_events
        (event_id,idempotency_key,revision,event_type,worker_id,actor_sha256,payload,
         recorded_at_ms,worker_generation)
        VALUES(:event,:idem,:revision,:type,:worker,:actor,CAST(:payload AS json),:now,
        :generation)"""),
        {
            "event": event_id,
            "idem": idempotency_key,
            "revision": revision,
            "type": event_type,
            "worker": worker_id,
            "actor": actor_sha256,
            "payload": json.dumps(payload or {}),
            "now": _now_ms(session),
            "generation": worker_generation,
        },
    )


def claim_worker(engine: Engine, worker_id: str) -> int:
    """Claim a short worker lease; only explicit existing worker invocation calls this."""
    if _UUID.fullmatch(worker_id) is None:
        raise SupervisorRejected("invalid_worker_id")
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        session.execute(
            text(
                "INSERT INTO paper_worker_runtime(id,state,revision,heartbeat_at_ms,"
                "lease_until_ms) "
                "VALUES(1,'STOPPED',0,0,0) ON CONFLICT (id) DO NOTHING"
            )
        )
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,lease_until_ms,generation "
                    "FROM paper_worker_runtime "
                    "WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one()
        )
        if row["state"] in ("RUNNING", "STOP_REQUESTED") and int(row["lease_until_ms"]) > now:
            raise SupervisorRejected("worker_lease_already_active")
        if int(row["generation"]) > 0:
            raise SupervisorRejected("legacy_worker_claim_cannot_replace_observer_generation")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET worker_id=:worker,state='RUNNING',revision=:rev,"
                "heartbeat_at_ms=:now,lease_until_ms=:until,manager_id=NULL,manager_pid=NULL,"
                "manager_start_token=NULL,process_pid=NULL,process_start_token=NULL,"
                "execution_mode='INERT' WHERE id=1"
            ),
            {"worker": worker_id, "rev": revision, "now": now, "until": now + LEASE_MS},
        )
        _append(
            session,
            event_id=f"worker_claim_{revision}",
            idempotency_key=None,
            revision=revision,
            event_type="WORKER_CLAIMED",
            worker_id=worker_id,
        )
        return revision


def heartbeat_worker(engine: Engine, worker_id: str) -> bool:
    """Refresh a live lease. Return false after a stop request; never revive expiry."""
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,lease_until_ms FROM paper_worker_runtime "
                    "WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["worker_id"] != worker_id:
            raise SupervisorRejected("worker_lease_lost")
        if row["state"] == "STOP_REQUESTED":
            return False
        if row["state"] != "RUNNING" or int(row["lease_until_ms"]) <= now:
            raise SupervisorRejected("worker_lease_expired")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET heartbeat_at_ms=:now,lease_until_ms=:until,"
                "revision=:revision WHERE id=1 AND worker_id=:worker"
            ),
            {"now": now, "until": now + LEASE_MS, "revision": revision, "worker": worker_id},
        )
        _append(
            session,
            event_id=f"worker_heartbeat_{revision}",
            idempotency_key=None,
            revision=revision,
            event_type="HEARTBEAT",
            worker_id=worker_id,
        )
        return True


def stop_requested(engine: Engine, worker_id: str) -> bool:
    with Session(engine) as session:
        row = (
            session.execute(text("SELECT worker_id,state FROM paper_worker_runtime WHERE id=1"))
            .mappings()
            .one_or_none()
        )
    return row is None or row["worker_id"] != worker_id or row["state"] != "RUNNING"


def release_worker(engine: Engine, worker_id: str) -> None:
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        now = _now_ms(session)
        row = (
            session.execute(
                text("SELECT worker_id,revision FROM paper_worker_runtime WHERE id=1 FOR UPDATE")
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["worker_id"] != worker_id:
            return
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET state='STOPPED',revision=:rev,"
                "heartbeat_at_ms=:now,lease_until_ms=0 WHERE id=1"
            ),
            {"rev": revision, "now": now},
        )
        _append(
            session,
            event_id=f"worker_stopped_{revision}",
            idempotency_key=None,
            revision=revision,
            event_type="WORKER_STOPPED",
            worker_id=worker_id,
        )


def request_worker_stop(
    engine: Engine,
    *,
    expected_revision: int,
    expected_generation: int | None = None,
    idempotency_key: str,
    actor_token: str,
) -> SupervisorStatus:
    if type(expected_revision) is not int or expected_revision < 0:
        raise SupervisorRejected("invalid_expected_revision")
    if (
        _IDEMPOTENCY.fullmatch(idempotency_key) is None
        or not actor_token
        or (expected_generation is not None and expected_generation < 0)
    ):
        raise SupervisorRejected("invalid_stop_request")
    actor = hashlib.sha256(actor_token.encode("utf-8")).hexdigest()
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": LOCK_ID})
        existing = (
            session.execute(
                text(
                    "SELECT revision,event_type,actor_sha256,payload "
                    "FROM paper_worker_supervisor_events "
                    "WHERE idempotency_key=:key"
                ),
                {"key": idempotency_key},
            )
            .mappings()
            .one_or_none()
        )
        if existing is not None and (
            existing["event_type"] != "STOP_REQUESTED"
            or existing["actor_sha256"] != actor
            or int(existing["payload"].get("expected_revision", -1)) != expected_revision
            or existing["payload"].get("expected_generation") != expected_generation
        ):
            raise SupervisorRejected("idempotency_key_conflict")
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,heartbeat_at_ms,lease_until_ms,generation,"
                    "manager_id,manager_pid,manager_start_token,process_pid,process_start_token,"
                    "execution_mode "
                    "FROM paper_worker_runtime WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SupervisorRejected("worker_runtime_uninitialized")
        generation = int(row["generation"])
        if generation > 0 and expected_generation != generation:
            raise SupervisorRejected("stale_worker_generation")
        if existing is None:
            if expected_revision > int(row["revision"]):
                raise SupervisorRejected("stale_worker_revision")
            if int(row["revision"]) != expected_revision:
                later_control_events = int(
                    session.execute(
                        text(
                            "SELECT count(*) FROM paper_worker_supervisor_events "
                            "WHERE revision > :revision AND event_type <> 'OBSERVER_HEARTBEAT'"
                        ),
                        {"revision": expected_revision},
                    ).scalar_one()
                )
                if later_control_events:
                    raise SupervisorRejected("stale_worker_revision")
            revision = int(row["revision"]) + 1
            state = "STOP_REQUESTED" if row["state"] in ("RUNNING", "STOP_REQUESTED") else "STOPPED"
            session.execute(
                text("UPDATE paper_worker_runtime SET state=:state,revision=:revision WHERE id=1"),
                {"state": state, "revision": revision},
            )
            _append(
                session,
                event_id=f"stop_{hashlib.sha256(idempotency_key.encode('ascii')).hexdigest()}",
                idempotency_key=idempotency_key,
                revision=revision,
                event_type="STOP_REQUESTED",
                worker_id=row["worker_id"],
                actor_sha256=actor,
                payload={
                    "requested_state": state,
                    "expected_revision": expected_revision,
                    "expected_generation": expected_generation,
                },
                worker_generation=int(row["generation"]) or None,
            )
        else:
            revision = int(row["revision"])
        now = _now_ms(session)
        generation = int(row["generation"])
        processes_verified = (
            generation > 0
            and process_matches(row["manager_pid"], row["manager_start_token"])
            and process_matches(row["process_pid"], row["process_start_token"])
        )
        active = (
            row["state"] in ("STARTING", "RUNNING", "STOP_REQUESTED")
            and int(row["lease_until_ms"]) > now
            and generation > 0
            and processes_verified
        )
        state = (
            "STOP_REQUESTED"
            if active
            else "STALE"
            if row["state"] in ("RUNNING", "STOP_REQUESTED")
            else "STOPPED"
        )
        return SupervisorStatus(
            state=state,
            revision=revision,
            worker_id=row["worker_id"],
            heartbeat_at_ms=int(row["heartbeat_at_ms"]),
            lease_until_ms=int(row["lease_until_ms"]),
            verified_running=state == "RUNNING" and active,
            stale=row["state"] in ("RUNNING", "STOP_REQUESTED") and not active,
            generation=generation,
            manager_id=str(row["manager_id"]) if row["manager_id"] else None,
            process_pid=int(row["process_pid"]) if row["process_pid"] else None,
            process_manager_verified=processes_verified,
            observation_only=row["execution_mode"] == "OBSERVATION_ONLY",
        )


def read_worker_status(engine: Engine) -> SupervisorStatus:
    with Session(engine) as session:
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,heartbeat_at_ms,lease_until_ms,generation,"
                    "manager_id,manager_pid,manager_start_token,process_pid,process_start_token,"
                    "execution_mode "
                    "FROM paper_worker_runtime WHERE id=1"
                )
            )
            .mappings()
            .one_or_none()
        )
        now = _now_ms(session)
    if row is None:
        return SupervisorStatus("STOPPED", 0, None, None, None, False, False)
    generation = int(row["generation"])
    processes_verified = (
        generation > 0
        and process_matches(row["manager_pid"], row["manager_start_token"])
        and process_matches(row["process_pid"], row["process_start_token"])
    )
    active = (
        row["state"] in ("STARTING", "RUNNING", "STOP_REQUESTED")
        and int(row["lease_until_ms"]) > now
        and generation > 0
        and processes_verified
    )
    stale = row["state"] in ("RUNNING", "STOP_REQUESTED", "STARTING") and not active
    state = "STALE" if stale else str(row["state"])
    return SupervisorStatus(
        state=state,
        revision=int(row["revision"]),
        worker_id=str(row["worker_id"]) if row["worker_id"] else None,
        heartbeat_at_ms=int(row["heartbeat_at_ms"]),
        lease_until_ms=int(row["lease_until_ms"]),
        verified_running=row["state"] == "RUNNING" and active,
        stale=stale,
        generation=generation,
        manager_id=str(row["manager_id"]) if row["manager_id"] else None,
        process_pid=int(row["process_pid"]) if row["process_pid"] else None,
        process_manager_verified=processes_verified,
        observation_only=row["execution_mode"] == "OBSERVATION_ONLY",
    )
