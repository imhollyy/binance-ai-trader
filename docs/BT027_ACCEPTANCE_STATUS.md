# BT-027 acceptance status

This branch adds guarded research and control APIs while keeping automatic
PAPER execution unavailable. All unknown information is represented as
unavailable rather than inferred.

## BT-027G — authoritative issue #106 acceptance status

This section is the current status after BT-027F at the BT-027G review point.
The earlier BT-027A/B and E3 checkpoints below are historical snapshots; where
they say START or the process manager is unsupported, the later BT-027F section
supersedes them. Production trading remains out of scope and disabled.

| Original issue #106 criterion | Evidence in this branch | Status |
| --- | --- | --- |
| Dashboard uses persisted PAPER data and does not invent an empty account | `/api/v1/paper/dashboard`; `PaperDashboardOverview`, `PaperSpotLedger`, `StagingViewer`; `paper-overview.spec.ts`, `staging-preview.spec.ts`, Full Stack PAPER E2E | PASS for truthful reads; unknown values remain unavailable |
| Bot state distinguishes verified process from preference/last result; START and STOP are safe | `/api/v1/paper/worker`, supervisor lease/generation and stop acknowledgement; `PaperWorkerSupervisor`; `paper_worker_supervisor_postgres.py` and Full Stack E2E | PASS for normal local API and isolated observation tests; staging viewer does not expose START and does not claim its last-result record is a process heartbeat |
| Risk levels 1–5 are versioned and controlled by independent RiskGuard | `/api/v1/risk/profiles`, `RiskCapabilitiesPanel`; staging viewer now reads this endpoint and no longer treats `/settings` as a RiskGuard profile | BLOCKED in the current private preview: no isolated RiskGuard database is configured there, so the endpoint reports unavailable and selection remains disabled. Local profile administration remains local-only. |
| Spot budget and ledger remain persistent and immutable | PAPER dashboard/ledger read API; append-only journal and database guards; `paper-ledger.spec.ts`, PostgreSQL ledger tests | PASS; no ledger reset or mutation was performed for this review |
| AI research shows real provenance, outcomes, approval status and recorded metrics only | `/api/v1/ai/research/predictions`; staging viewer now reads the authenticated endpoint; `AiResearchPanel`; `test_bt027_research_risk_api.py`; staging Playwright fixtures | PASS for provenance-safe UI and no-data behavior. Current preview has no isolated research source/metrics, so it must show unavailable/empty and `RESEARCH ONLY — NOT APPROVED`; no performance is claimed. |
| Charts use recorded, closed Spot candles with timestamps | stored candle API and `StoredCloseChart`; `recorded-charts.spec.ts`, staging console Playwright | PASS; no live-price label or fabricated connecting value |
| Portfolio/history separate gross/net P&L and costs; unknown spread is not zero | persisted Decimal-string PAPER events and ledger view; `paper-ledger.spec.ts` checks gross/net, recorded fees/slippage and unknown spread | PASS for supported records; mark-to-market remains unavailable without a trusted mark |
| Mobile/desktop, empty/error/loading and accessible navigation | Playwright 360/390/768/1280 and keyboard/reduced-motion tests; staging tests exercise login, panels and empty/error states | PASS locally on this review head (26 default UI tests; 18 staging-viewer tests) |
| Password/session/admin values are absent from browser persistent storage, URL and staging bundle | staging viewer keeps session material in component state and uses the server cookie; Playwright asserts empty local/session storage and no credential in URL; staging production bundle scan rejects local admin token markers | PASS locally for tested browser flows and staging bundle scan; no secret was printed |
| Preview is private/local and usable by the owner | loopback-only ingress and exact-host Caddy routing in `compose.preview.yaml` / `Caddyfile.quick`; private HTTPS smoke workflow | BLOCKED for manual acceptance: local port `127.0.0.1:8765` currently returns 404 without the generated allowed Host, and the current worktree has no Quick Tunnel origin log/PID record. Existing containers alone do not provide a usable URL. |
| Auth, Origin/CSRF, roles, idempotency, audit and fail-closed behavior | `staging_auth.py`, staging auth/API/HTTPS ingress tests, profile/worker integration tests, preview preflight and CI | PASS in automated tests; user review of the current private preview is still pending |

### BT-027G browser acceptance and remaining blockers

On the BT-027G worktree, the default Playwright suite passed 26 tests and
skipped 23 fixture-gated cases; the opt-in staging UI suite passed 18/18 after
providing only a mock HTTPS API origin. The 23 default skips include staging
tests that were separately run and passed, plus tests requiring the isolated
connected backend. The Full Stack PAPER E2E workflow is the authoritative
browser-to-API-to-PostgreSQL test for those connected cases; on the current
BT-027G head this connected workflow passed. Do not report a local skip as a
pass. All five required Actions passed on commit `9acf10d`.

Two original issue #106 acceptance blockers remain: (1) the private preview has
no configured, independent RiskGuard database, so its real 1–5 profile cannot
be selected there; (2) a current private preview URL could not be verified and
manual browser acceptance has not happened. Do not merge or mark BT-027 accepted
until these are resolved/reviewed. This is distinct from future production
automatic BUY, independent positive-net-edge approval, scheduler activation,
model approval, Binance private orders and Futures, all of which remain disabled
and are not requirements for original issue #106.

Current safe owner access path (not yet available in this checkout): use the
HTTPS Quick Tunnel URL printed by `Start-TradePilotQuickPreview.ps1`; Cloudflare
requires the explicitly allowed email's PIN, followed by the separate preview
viewer password. The URL is ephemeral and must not be guessed. The script must
be reviewed against the existing `tradepilot_quick` project/volume before a
restart because this review intentionally did not restart or rebuild the
current preview. Browser checklist: (1) sign in as viewer; (2) verify empty
ledger is not rendered as zero capital; (3) inspect recorded candle timestamps;
(4) inspect Risk Management and confirm unavailable RiskGuard is disabled;
(5) inspect AI Research and confirm `RESEARCH ONLY — NOT APPROVED` plus empty/
unavailable data and no fabricated metrics; (6) inspect worker state and ensure
it is never shown RUNNING without a verified heartbeat; (7) check history costs
and unknown spread; (8) confirm no local admin token field, no START, no data in
browser storage/URL; (9) sign out. Use only the isolated preview account.

## Risk profiles (BT-027A)

`GET/PUT /api/v1/risk/profiles` reads and writes complete, versioned RiskGuard
profiles in the separately isolated RiskGuard database. A write requires the
normal authorized write path, an explicit confirmation field and confirmation
header, the current runtime revision, and a stopped worker. Every install is
audited with a hashed actor identifier. A PostgreSQL advisory lock serializes
profile changes with worker claims. Missing profile values are never filled in.
This configures RiskGuard; it does not authorize or activate trading.

## Worker status and stop (BT-027B historical baseline; superseded by BT-027F)

`GET /api/v1/paper/worker` reports database-backed worker identity, heartbeat,
lease, revision and stale state. Authorized, idempotent STOP requests record an
append-only audit event and remain `STOP_REQUESTED` until the worker observes
the request and releases its lease. An expired lease is reported as stale.
At this BT-027B checkpoint there was no START endpoint. BT-027F later added an
observation-only child lifecycle under exact disposable database sentinels;
normal local/staging configurations still do not expose START. A heartbeat
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

The local dashboard has a user-confirmed independent profile form, worker
status/STOP request, and stored research receipt view. The staging viewer now
reads the independent RiskGuard and AI research APIs, showing unavailable data
without using application settings as a substitute. Normal staging START stays
disabled; the disposable observation lifecycle is only exposed by exact test
sentinels. Research metrics remain unavailable unless persisted evidence exists.

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

### BT-027F observation-only process lifecycle (isolated acceptance)

Migration `0023_bt027_observer_fencing` adds a monotonic runtime generation,
manager UUID/PID/process-start token, child PID/process-start token and an
explicit `OBSERVATION_ONLY` execution mode. Supervisor events bind to the
generation. START claims serialize under the supervisor advisory lock and
runtime row lock; only one fresh generation can be claimed for the expected
revision. STOP requires the current generation, persists an idempotent audited
request, and remains pending until the child observes it and appends its STOP
acknowledgement. Heartbeat and STOP acknowledgements compare worker ID and
generation; expired or stale generations cannot revive a lease. A legacy
generationless worker claim is not process-attested and cannot replace an
observer generation.

`PaperObservationProcessManager` launches a separate Python child process.
That child imports only process-identity and supervisor/database modules; it
does not import market data, strategy, ledger, RiskGuard or exchange code. It
attests its own OS process-start identity, sends database-backed heartbeats,
and exits after STOP acknowledgement. The manager detects child exit; the
child detects manager loss and records failure. Linux process identity rejects
zombie processes, and Windows identity checks the process exit code as well as
the creation time so a terminated PID is not considered live.

The START route is registered only when both explicit observer/supervisor test
sentinels are set and `DATABASE_URL` exactly matches the provided SQLAlchemy
engine for loopback PostgreSQL database `trader_worker_supervisor_test`.
Staging, preview and normal application configurations do not register START;
the UI keeps it disabled when the API reports `start_supported=false`. In the
isolated test mode the only accepted mode is `OBSERVATION_ONLY`. This is an
acceptance harness, not a deployable production process supervisor or a
production START capability. START never grants trading permission.

The disposable PostgreSQL 16 integration suite ran the migration from an
empty database and passed 2 tests without skips. It exercised authorized and
unauthorized START/STOP, untrusted Origin rejection, same-request replay and
payload conflict, duplicate and concurrent controller claims from separate
OS processes, child crash and generation replacement, manager-process crash
and automatic child failure, manager restart/replay without a new generation,
STOP racing START while heartbeat is active, expired lease and delayed heartbeat
rejection, stale generation rejection, legacy claim denial after observer
generation, a protocol relay dropping live PostgreSQL connections, safe failure
and idempotent restart after the outage, and unchanged PAPER journal/commit-
receipt counts. No PAPER BUY, strategy scheduler or exchange request is made
by this observation worker.

The common PAPER ledger entry guard remains fail-closed and worker-origin
coordinator intents still reject because durable supervisor fencing has not
been integrated into the separate E3 entry authorization contract. During the
tested database connection outage, the observation child exited on its database
error; once connectivity returned, the manager persisted failure and replayed
the same START without allocating a new generation. The test drops database
connections through a local TCP relay while PostgreSQL itself remains running;
host/server crash behavior is not claimed. The process manager is hosted inside
the local API process rather than an independently supervised service, so
host-level process supervision/HA is not claimed. Production PAPER BUY,
automatic strategies, scheduler activation, AI model execution, Binance
private orders and Futures remain disabled. BT-027F's isolated observation
lifecycle is implemented; overall BT-027 and production execution acceptance
remain open pending the separate dashboard/security and BUY authorization
gates.
