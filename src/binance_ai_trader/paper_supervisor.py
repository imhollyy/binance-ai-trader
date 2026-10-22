"""Database-backed worker identity and stop signalling; no start authorization."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

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
) -> None:
    session.execute(
        text("""INSERT INTO paper_worker_supervisor_events
        (event_id,idempotency_key,revision,event_type,worker_id,actor_sha256,payload,recorded_at_ms)
        VALUES(:event,:idem,:revision,:type,:worker,:actor,CAST(:payload AS json),:now)"""),
        {
            "event": event_id,
            "idem": idempotency_key,
            "revision": revision,
            "type": event_type,
            "worker": worker_id,
            "actor": actor_sha256,
            "payload": json.dumps(payload or {}),
            "now": _now_ms(session),
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
                    "SELECT worker_id,state,revision,lease_until_ms FROM paper_worker_runtime "
                    "WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one()
        )
        if row["state"] in ("RUNNING", "STOP_REQUESTED") and int(row["lease_until_ms"]) > now:
            raise SupervisorRejected("worker_lease_already_active")
        revision = int(row["revision"]) + 1
        session.execute(
            text(
                "UPDATE paper_worker_runtime SET worker_id=:worker,state='RUNNING',revision=:rev,"
                "heartbeat_at_ms=:now,lease_until_ms=:until WHERE id=1"
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
    idempotency_key: str,
    actor_token: str,
) -> SupervisorStatus:
    if type(expected_revision) is not int or expected_revision < 0:
        raise SupervisorRejected("invalid_expected_revision")
    if _IDEMPOTENCY.fullmatch(idempotency_key) is None or not actor_token:
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
        ):
            raise SupervisorRejected("idempotency_key_conflict")
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,heartbeat_at_ms,lease_until_ms "
                    "FROM paper_worker_runtime WHERE id=1 FOR UPDATE"
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise SupervisorRejected("worker_runtime_uninitialized")
        if existing is None:
            if int(row["revision"]) != expected_revision:
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
                payload={"requested_state": state, "expected_revision": expected_revision},
            )
        else:
            revision = int(row["revision"])
        now = _now_ms(session)
        active = row["state"] in ("RUNNING", "STOP_REQUESTED") and int(row["lease_until_ms"]) > now
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
        )


def read_worker_status(engine: Engine) -> SupervisorStatus:
    with Session(engine) as session:
        row = (
            session.execute(
                text(
                    "SELECT worker_id,state,revision,heartbeat_at_ms,lease_until_ms "
                    "FROM paper_worker_runtime WHERE id=1"
                )
            )
            .mappings()
            .one_or_none()
        )
        now = _now_ms(session)
    if row is None:
        return SupervisorStatus("STOPPED", 0, None, None, None, False, False)
    active = row["state"] in ("RUNNING", "STOP_REQUESTED") and int(row["lease_until_ms"]) > now
    stale = row["state"] in ("RUNNING", "STOP_REQUESTED") and not active
    state = "STALE" if stale else str(row["state"])
    return SupervisorStatus(
        state=state,
        revision=int(row["revision"]),
        worker_id=str(row["worker_id"]) if row["worker_id"] else None,
        heartbeat_at_ms=int(row["heartbeat_at_ms"]),
        lease_until_ms=int(row["lease_until_ms"]),
        verified_running=row["state"] == "RUNNING" and active,
        stale=stale,
    )
