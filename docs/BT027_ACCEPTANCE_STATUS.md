# BT-027 acceptance status

This branch adds guarded research and control APIs while keeping automatic
PAPER execution unavailable. All unknown information is represented as
unavailable rather than inferred.

## Risk profiles (BT-027A)

`GET/PUT /api/v1/risk/profiles` reads and writes complete, versioned RiskGuard
profiles in the separately isolated RiskGuard database. A write requires the
normal authorized write path, an explicit confirmation field and confirmation
header, the current runtime revision, and a stopped worker. Every install is
audited with a hashed actor identifier. A PostgreSQL advisory lock serializes
profile changes with worker claims. Missing profile values are never filled in.
This configures RiskGuard; it does not authorize or activate trading.

## Worker status and stop (BT-027B)

`GET /api/v1/paper/worker` reports database-backed worker identity, heartbeat,
lease, revision and stale state. Authorized, idempotent STOP requests record an
append-only audit event and remain `STOP_REQUESTED` until the worker observes
the request and releases its lease. An expired lease is reported as stale.
There is deliberately no START endpoint or scheduler activation. A heartbeat
does not grant trading authority.

## AI research API (BT-027C)

`GET /api/v1/ai/research/predictions` reads only prospective receipts and
persisted outcomes from the configured isolated research database. It returns
stored provenance and keeps every model `RESEARCH_ONLY_NOT_APPROVED`. If the
database is absent, the endpoint returns an unavailable empty result. No
performance metric is reported unless persisted evaluation evidence exists;
the current response explicitly reports metrics unavailable where it does not.
This API has no execution capability.

## Web integration and remaining acceptance

The dashboard contains an explicit user-confirmed profile form, verified worker
status with STOP request, and stored research receipt view. These views fail
closed when their APIs or isolated stores are unavailable. START remains
disabled because a complete safe start contract is not implemented. The
research database currently has no persisted performance metric evidence.
PostgreSQL integration tests must run in their disposable CI services; they are
not claimed as local passes when those services are absent. BT-027 must remain
unmerged until the disposable PostgreSQL, required CI, and browser checks all
pass and the remaining acceptance decision is reviewed.

## BT-027E / BT-027F security integration

Persistent entry adapters (manual BUY, observed-strategy BUY, and signal-plan
BUY) fail closed with `riskguard_paper_atomicity_unavailable`. The common
`open_paper_spot` ledger transaction now applies the same gate after replay
detection, preventing new callers from bypassing the adapters. Only an exact
localhost PostgreSQL fixture URL, a named disposable database and explicit
`PAPER_LEDGER_TEST_ISOLATED=1` allow ledger mechanics to run in tests.
This exception is not present on normal PAPER or preview databases. RiskGuard
reservations live in its isolated PostgreSQL database while the PAPER ledger
lives in the application database. No distributed commit or recoverable
reservation protocol currently binds the two writes, so RiskGuard approval
cannot safely authorize a ledger mutation. The entry gate runs in the locked
ledger preflight after idempotent replay detection; persisted receipts remain
replayable. Existing position reductions retain their existing price and
account checks. Offline/test-only ledgers are not production entry adapters.

Persistent START and execution activation remain unsupported; the UI must keep
START disabled. Lease identity, heartbeat, stale detection, authorized
idempotent STOP request, and stop acknowledgement are control/status primitives
only. They do not grant entry authority or start an execution process. Enabling
START still requires a process manager with persisted fencing-token enforcement
at the execution boundary.
