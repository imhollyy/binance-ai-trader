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


## BT-027E recovery classifier (inert groundwork)

`paper_entry_recovery.py` now defines strict immutable, digest-bound evidence
types and a conservative cross-store comparison. It is deliberately **not**
connected to any trading or mutation route. The classifier never authorizes a
new BUY, releases a reservation, or treats two caller-reported digests as
authenticated proof of a distributed commit.

| Risk reservation | PAPER journal | Recovery result |
| --- | --- | --- |
| Missing | Missing | No evidence; new entry still forbidden |
| Present | Missing | Hold the reservation; investigate/reconcile |
| Missing | Present | Orphan journal; investigate ledger and authorization |
| Present | Present, mismatch/released | Integrity conflict; investigate |
| Present | Present, matching metadata | Still unverified; independent attestation needed |

Required before enabling entries: write-ahead intent recorded durably prior
to reservation; immutable intent hash and exact Decimal amounts bound to both
stores; idempotent reserve/commit/release operations; recovery after every
crash boundary; protection against concurrent retries, stale profile,
STOP/lease-generation changes, and model revocation; attested ledger snapshots;
and an operational circuit breaker on reconciliation uncertainty. The current
`risk_store.evaluate_and_reserve` and PAPER journal do not yet expose a
complete cross-store, recovery-safe transaction protocol. The existing
`open_paper_spot` denial remains active. START remains unsupported.

### BT-027E1 durable intent journal

Migration `0019_bt027_paper_entry_intents` adds an immutable PAPER application
database intent record and append-only state-event history. The coordinator
hashes canonical, exact Decimal intent fields, deduplicates by idempotency key,
serializes state transitions under a row lock, and exposes nonterminal intents
for restart recovery review. Database triggers enforce immutable intent rows,
append-only audit events, contiguous state history, and the declared transition
graph. This is an intent journal only: it does not itself reserve capacity,
commit a PAPER ledger mutation, or authorize BUY. `RECONCILIATION_REQUIRED`
is terminal in this stage until a later independently attested recovery contract
exists. Caller supplied ledger/cost hashes are stored as claims, not verified
accounting evidence. START remains disabled.

### BT-027E2P account and market evidence

Migration `0020_bt027_entry_evidence` adds append-only application-database
receipts for a ledger-reconciled PAPER account snapshot and fresh public
pre-entry market observations. The account hash covers settings, cash, realized
net P&L, commissions, positions and ordered journal rows. Market evidence binds
a closed 1m public observation, bookTicker/depth coherence, observed spread and
versioned hypothetical fee/slippage assumptions. Those assumptions do not
represent an account fee tier or actual execution costs; expected net advantage
remains explicitly unverified.

The coordinator rechecks current account state and stored evidence under PAPER
database locks, then requires RiskStore's independently persisted reservation,
audit event, current profile/runtime, exact risk request and matching account
snapshot before appending `RISK_RESERVED`. This verifies a risk reservation, not
distributed atomicity or permission to BUY. AI intents remain blocked, and
START remains disabled.

The next E2 coordinator can build on `create_intent`, `get_intent`,
`transition_intent`, `list_status_events`, `list_recovery_candidates`, and
`assess_cross_store_evidence`. The durable contract is `request_id` plus unique
`idempotency_key` and canonical intent SHA-256, immutable amount/version/source
fields, account/settings-version binding, and serialized append-only event
sequence with a database-enforced transition graph. E2P adds independently
verified RiskGuard reservation references and account/cost receipt attestation.
These receipts alone must not authorize a ledger write. BT-027E3 adds an
isolated, disposable-test-only ledger commit coordinator and append-only commit
receipt; it does not authorize production entries.

### BT-027E3T recovery verification status

Recovery revalidates the immutable commit receipt against the intent hash,
RiskGuard event identifiers, original BUY journal event, account/position,
idempotency key, quantity, fee, and cash-balance arithmetic. It accepts a
position that was legally closed after its BUY and verifies the completed
receipt again on replay. Disposable dual-database tests cover RiskGuard outage
with a held reservation, rollback after ledger rows and receipt are written
but before transaction commit, restart after ledger commit, response loss
after COMPLETED, post-close replay, AI-intent rejection, and ledger
immutability. Separate RiskGuard PostgreSQL tests cover same-ID concurrent
retries and different IDs competing for a limited budget.

#### BT-027E3T3 crash and outage matrix

Executed in the explicitly whitelisted disposable application and RiskGuard
PostgreSQL databases. The tests use separate Python child processes, hard
process termination (`Popen.kill`, SIGKILL on POSIX / process termination on
Windows), newly constructed SQLAlchemy engines after restart, and temporary
database locks. Test profile limits are explicitly confirmed fixture values
written only to the disposable RiskGuard database.

| Boundary / fault | Result | Evidence and limit |
| --- | --- | --- |
| Before intent persistence | PASS | Child is killed before write; idempotency key has no intent/event and PAPER ledger is unchanged. |
| After PREPARED | PASS | Child persists intent, is killed, and a new engine reads the immutable PREPARED event; no ledger mutation. |
| During RiskGuard reservation transaction | PASS | Separate PostgreSQL session holds an audit table lock; child is observed waiting in PostgreSQL, then killed. Transaction rolls back with zero audit/reservation rows. |
| After RiskGuard reservation commit, before app RISK_RESERVED | PASS | Child is killed after its reservation call returns. Restart finds exactly one verified hold; app remains PREPARED and no RELEASE is written. |
| After app RISK_RESERVED event | PASS | Child is killed after the state event commits; a new engine reads RISK_RESERVED. |
| During PAPER ledger transaction | PASS | Disposable-only journal INSERT trigger blocks the actual coordinator transaction. Child is killed while PostgreSQL reports a lock wait; account cash, position/journal counts and RISK_RESERVED remain unchanged, and the hold is retained. |
| After PAPER commit, before COMPLETED | PASS | Child returns at the explicit test-only after-commit boundary, then is killed. New engine verifies the immutable receipt and completes recovery; repeated commit creates no second BUY. |
| After COMPLETED | PASS | Completed replay child is killed after returning; restart preserves COMPLETED and exact ledger counts. |
| RiskGuard database unavailable | PASS | Disposable DB refuses connections after existing sessions are terminated; no release occurs. Once restored, the original reservation verifies. |
| PAPER application database unavailable | PASS | Disposable DB refuses connections after existing sessions are terminated; no ledger mutation occurs. Once restored, the PREPARED intent and ledger counts persist. |
| Same RiskGuard ID/hash across processes | PASS | Two gated child processes retry concurrently; one original audit/reservation remains. A separate process with changed payload is rejected as a conflict. |
| Different request IDs competing for one PAPER account | PASS | Existing three-caller PostgreSQL race (two identical calls plus another request) produces at most one BUY, position and commit receipt; concurrent callers are threads in one process, not separate OS processes. |
| STOP/profile revision | PASS | RiskGuard verification rejects PAUSED state and a reservation tied to an older active profile. No release event is generated. |
| Worker fence | PASS for denial | Any intent containing worker identity/generation is rejected by the test coordinator because a DB-backed supervisor fence is not implemented. Valid worker execution is not claimed. |

At the T3 checkpoint, protocol-level COMMIT acknowledgement loss and PostgreSQL
server restart had not yet been tested; the T4 results below supersede that
gap. A risk or PAPER database outage is tested as an actual refused connection
with backend sessions terminated, not as host or storage failure. Corrupt
receipt/journal rows cannot be updated through normal SQL because append-only
guards reject such writes; coordinator and migration tests reject
forged/mismatched receipts/events. No trigger bypass or replication-role
mutation is used to manufacture corrupted history.

#### BT-027E3T4 final fault-injection slice

Executed on disposable PostgreSQL 16 instances. The first pair of databases
uses separate `trader_paper_entry_coordinator_test` and
`trader_paper_entry_risk_test` databases on a task-owned container. The server
crash test creates its own uniquely named PostgreSQL 16 container and named
volume with a unique ownership label, then removes only those exact resources.
The coordinator's alternate test-port exception requires an explicit T4
sentinel, localhost, and the exact two test database names; ordinary production
validation remains unchanged.

| Scenario | Result | Evidence |
| --- | --- | --- |
| Server committed, client missed COMMIT acknowledgement | PASS | A PostgreSQL protocol-aware TCP proxy observed the backend `CommandComplete(COMMIT)` while `synchronous_commit=on`, withheld and dropped that response. A separate direct engine read the single immutable commit receipt, position, BUY journal, `LEDGER_COMMITTED` event, and reduced cash before the client received a connection error. A new engine recovered to `COMPLETED`; replay kept the same journal/position/receipt counts, exactly one RiskGuard RESERVE, and no RELEASE. |
| PostgreSQL process killed before ledger commit | PASS | In a dedicated PG16 container and temporary labeled volume, the coordinator was observed blocked inside the ledger journal transaction. The server process was killed and restarted with the same volume. WAL recovery exposed no partial BUY/receipt/account change; the intent remained `RISK_RESERVED` and its reservation was held. |
| PostgreSQL process killed after commit, before client acknowledgement | PASS | The protocol proxy held the COMMIT completion after server commit; independent read-back established the committed rows, then the actual PG16 process was killed and restarted on the same volume. New engines read all committed ledger/receipt/event rows, completed recovery, and replay created no second BUY or reservation. |
| Different keys, two OS processes, one shared insufficient account balance | PASS | Two child Python processes used separate app and RiskGuard engines and idempotency keys. PostgreSQL `pg_stat_activity` showed both blocked on the same PAPER settings/account row locks before release. Exactly one committed BUY/position/receipt; the other failed closed after seeing the open position without a current mark. Two independently persisted RESERVEs remained held, zero RELEASE events were written, and account cash/journal/position counts reconciled. |
| Same-key replay and changed payload | PASS | The completed request replay resolved to the original request; changed quantity/notional on its idempotency key raised `idempotency_key_payload_conflict`. |

The server-crash test covers a deterministic pre-commit lock wait and a
post-commit/pre-ack crash. It does not claim deterministic interruption during
the internal WAL flush interval. No distributed ACID transaction is claimed
across application and RiskGuard databases; ambiguous reservations are held for
reconciliation. The full coordinator integration file passed 14 tests with no
skips on two disposable databases; the dedicated T4 subset passed 2 tests.

E3's isolated coordination/recovery evidence now covers the T4 acceptance
matrix, but this does not authorize production BUY. Expected net advantage is
still unverified independently, and a persisted worker fencing proof is not
implemented; worker-origin requests remain rejected. BT-027F still requires a
real process manager, durable lease/generation fencing enforced at the
execution boundary, and verifiable START/STOP completion. Production BUY,
START, scheduler activation, live orders, Futures, and AI model activation
remain disabled.

E3 remains incomplete. Production BUY stays disabled: expected net advantage is
not independently verified, and the two stores have no distributed ACID
transaction. Worker intents are fail-closed because there is no persisted
supervisor fencing proof at the execution boundary. BT-027F process-manager
fencing and START remain separate acceptance gates.

### BT-027E3T2 transition evidence guard

Migration `0022_bt027_entry_guard` strengthens the application
database event trigger. `LEDGER_COMMITTED` and `COMPLETED` now require the event
evidence reference to equal an immutable receipt hash whose request and intent
match the intent row and whose journal, position, account, idempotency key,
quantity, fee, and cash movement match the immutable PAPER ledger records.
`transition_intent()` refuses these two states regardless of a caller-provided
text reference; only the ledger commit callback and receipt recovery path may
append them. The PostgreSQL trigger also rejects direct SQL event inserts that
lack a matching ledger receipt.

The trigger runs only in the application database. It cannot query or prove the
independent RiskGuard database. The coordinator must verify the actual
RiskGuard reservation and audit before it enters `RISK_RESERVED`; the local
trigger verifies only the persisted RiskGuard reference identifiers in the
ledger receipt. The trigger also checks that the receipt hash matches the
event reference, but does not recompute the application's canonical SHA-256
encoding. Production BUY remains disabled.
