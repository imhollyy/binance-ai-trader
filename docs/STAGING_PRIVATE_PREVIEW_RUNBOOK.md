# BT-009B — Private staging preview: ingress, identity and release gate

**Status: DESIGN / NOT DEPLOYED.** This document is a proposed security
boundary and a set of falsifiable acceptance checks for issue #63. It is
**not** a production configuration, installation guide or authorization to
provision Cloudflare, DNS, paid infrastructure, secrets or real users.

Related: [Issue #63](https://github.com/imhollyy/binance-ai-trader/issues/63),
[Issue #37](https://github.com/imhollyy/binance-ai-trader/issues/37),
[STAGING_AUTH.md](STAGING_AUTH.md), and [AGENTS.md](../AGENTS.md).

## 1. Invariants and current reality

- `dev` only; `main`, Tam Kare, real users and production databases untouched.
- Binance PUBLIC Spot market data and PAPER accounting **only**. No real
  orders, private API keys, Futures execution or exchange credentials.
- PAPER scheduler stays **OFF**. Persisted `bot_enabled` is not process
  evidence, and deployment must not start the optional scheduler.
- Existing `compose.yaml` is **LOCAL ONLY**, injects `ADMIN_TOKEN` and
  binds the API to loopback. **Never publish this Compose stack** through a
  tunnel or as an internet-facing staging environment.
- `API_AUTH_MODE=staging` rejects an `ADMIN_TOKEN`; it requires an
  isolated PostgreSQL DB, two distinct hashed passwords and exact HTTPS
  origin/hostname. The legacy token must never reach staging.
- Currently no Cloudflare Access policy, tunnel, staging ingress, TLS,
  hosted database, actual HTTPS browser-cookie roundtrip or backup/restore
  proof has been deployed or verified. Browser CI for BT-009A used a mock
  HTTPS API; it is **not** a real edge-to-API authentication test.

## 2. Proposed boundary for review (not yet selected/deployed)

Prefer evaluating **one HTTPS hostname** with path routing, to remove
cross-subdomain Access/CORS and SameSite cookie ambiguity:

```text
Private, approved test user / browser
  | HTTPS  [Access login + conditional MFA, WAF, rate limits]
  v
Cloudflare edge: https://preview.staging.example (ILLUSTRATIVE ONLY)
  | outbound-only Tunnel (no publicly reachable origin listener)
  v
Restricted staging ingress / reverse proxy
  |-- "/" and Next assets -> staging Next.js web UI
  |-- "/api/v1/*"       -> STAGING FastAPI
  `-- health/internal   -> deny external access
                              |
                   STAGING-ONLY PostgreSQL (private network)
```

The hostname above is documentation-only; it is **not** a DNS record.
Both `STAGING_WEB_ORIGIN` and `NEXT_PUBLIC_STAGING_API_ORIGIN` can
point to the same `https://preview.staging.example`, with
`STAGING_API_HOST=preview.staging.example`. Verify actual Next.js
route behavior and that `/api/v1/*` always reaches FastAPI before
selecting this topology. Existing BT-009A examples used **two** distinct
same-site hostnames; that remains an alternative only after a real
preflight-cookie/Access/CORS test. No public API bypass of Access is allowed.

**Unresolved ingress decision:** `staging_auth.StagingAccess._origin`
requires `request.url.scheme == "https"`; the repository's Dockerfile
starts Uvicorn with HTTP on port 8000. Cloudflare-side HTTPS alone does
**not** establish that FastAPI sees HTTPS. Before ANY preview publish,
implement and verify one of:

1. Private end-to-end TLS to the FastAPI listener, including verified
   certificates and private routing, so ASGI sees HTTPS; **or**
2. A single authenticated, network-isolated ingress which removes all
   client-supplied `Forwarded`/`X-Forwarded-*` and replaces them from
   a verified TLS request, with ASGI proxy-header trust restricted to
   that exact ingress socket/IP (never `*` or an open origin).

Do not assume Uvicorn default proxy settings, Cloudflare-generated
headers or browser-supplied forwarded headers are proof of TLS.
Reject unapproved scheme, hostname and Origin at the API; reject direct
origin access and bypass of Access even if the requester knows the
origin IP or sends `Host`/`CF-Connecting-IP` headers. In particular,
**do not** disable `TrustedHostMiddleware`, weaken the exact Origin
comparison, set permissive CORS wildcards or copy the local Compose
`ADMIN_TOKEN` into a staging container.

**Platform decision pending:** a private Next.js container behind Tunnel
is the simplest one-host candidate; a Cloudflare Pages-hosted UI plus
separate API may also be evaluated, but would require evidence for API
Access-cookie preflights, `SameSite=Strict` and public build variables.
Do not deploy either as part of this PR.

## 3. Data and secret isolation contract

- New, empty, disposable staging-only PostgreSQL database, isolated
  credentials, service identity, network, backup target and retention
  from any existing personal/production PAPER journal. No copy of
  personal data or credentials; fixtures must be synthetic.
- Apply additive migration `0006_staging_sessions` only to that database
  after confirming target connection identity without printing
  `DATABASE_URL`. Capture pre/post schema and migration revision.
- A staging-only secret store provides salted scrypt verifier hashes,
  database credentials and TLS/Access secrets to the correct workloads.
  No raw passwords, cookies, CSRF values or DB URLs in the browser,
  Next.js `NEXT_PUBLIC_*` vars, source tree, GitHub Actions logs,
  access/error logs, response bodies or issue/PR comments.
- `NEXT_PUBLIC_TP_STAGING_PREVIEW=enabled` is a reviewed *build-time*
  switch; only a public HTTPS API origin may be public. Scan built
  JavaScript and runtime environment for exposed credentials **without
  outputting the secrets themselves**.
- Set explicit backup frequency, encrypted storage, retention and
  recovery objectives with the user **before deploy**; do not
  fabricate an RPO/RTO. Practice a full restore into a second disposable
  database, check schema revision and PAPER journal invariants, and
  perform an isolated failure/rollback drill. Never run
  `docker compose down -v` against any persistent database.
- Define expired/revoked session cleanup and password/secret rotation,
  including forced session revocation, before real beta users are onboarded.

## 4. Required integration and abuse evidence

Record each case with status `PASS`, `FAIL` or `NOT RUN`, target
(environment label, **not** secret host), commit SHA and sanitized evidence.
Unit tests and mock browser tests must be identified separately from
actual HTTPS/browser/database integration.

| ID | Acceptance check | Required result |
|---|---|---|
| ING-01 | Anonymous request or direct bypass to origin, missing Access | Denied; no origin route |
| ING-02 | Browser → Access → HTTPS proxy → FastAPI (real browser) | Successful viewer session over valid TLS |
| ING-03 | HTTP, wrong Host, wrong Origin, spoofed Forwarded/X-Forwarded-* | Rejected; no session created |
| ING-04 | Cross-site preflight/Access interaction if using two hostnames | Strict allowlist; login, credentials and CSRF work, or fail closed |
| AUTH-01 | Invalid/expired/revoked cookie and replay after logout/restart | 401; no protected content |
| AUTH-02 | Wrong CSRF, stolen cookie alone, wrong role, forged approval | No PAPER settings change |
| AUTH-03 | Five failed login attempts, distributed edge abuse | Server lockout and edge limits proved; no account enumeration |
| WEB-01 | Mobile 360/390/768/1280, keyboard, error/logout/session expiry | Usable, no sensitive response caching |
| DATA-01 | Fresh isolated DB; schema upgrade and restore test | 0006 additive; no user/production data touched |
| DATA-02 | Backup encryption/retention and recovery drill | Restored and reconciled synthetic PAPER journal |
| SEC-01 | Secret and cookie redaction, no secret in web bundle | Negative scans; no plaintext disclosure |
| OPS-01 | Access/WAF policy, ingress health, alarms and deny-by-default | Sanitized logs and tested incident/rollback plan |
| SAFE-01 | Scheduler, real exchange orders, API private keys, Futures routes | No runtime activation or execution capability |

The current BT-009A test suite verifies portions of `AUTH-01/02/03`
against isolated local fixtures only. **All real-edge rows above remain
NOT RUN** until a separate approved staging deployment and test.

## 5. Rollout, shutdown and incident plan (approval gated)

**Before creating any provider resources** record a separate explicit
decision covering Cloudflare account/zone, domain/DNS, server/database
provider, access list, projected recurring costs, authorized operator
and whether the preview should be time-limited. The reviewer must
confirm `dev` SHA and all four GitHub Actions workflows on the final
PR head. An approved infrastructure/deployment PR is a **separate issue**.

Proposed gated order:

1. Review threat model and authorize disposable private staging only.
2. Provision dedicated secrets, isolated DB/backup, private ingress and
   Access rules with deny-by-default routing and TLS. No `main` release.
3. Verify backend scheme/host/origin against actual HTTPS and adversarial
   header requests **before** revealing the preview to beta participants.
4. Test one viewer and one operator using synthetic accounts and the
   matrix above; record failures honestly, then correct and rerun.
5. Verify restore/retention, alarms, rotation, response procedures and
   shutoff under a simulated bad deploy; only then consider private beta.

**Emergency shutoff (design, not executed):** revoke Cloudflare Access
policy/tunnel route, disable the staging ingress, stop **staging-only**
containers (not the shared host/database), revoke sessions and rotate
staging credentials; keep encrypted backups and immutable audit evidence.
Document operator, escalation, impact, time and reconciliation. Do not
assume disabling `bot_enabled` shuts down a running process: scheduler
is disabled and independently controlled by the runtime.

## 6. Exit criteria for this preparatory PR

- This document reviewed alongside `docs/STAGING_AUTH.md` and current
  `api_config.py`, `api.py`, `staging_auth.py`, `compose.yaml`.
- No Cloudflare, DNS, server, user, secret, `main` or deployment changes.
- Architecture and tests are proposals, **not** a security attestation.
- Any added test/implementation package must have its own PR and actual
  isolated CI/HTTPS evidence before issue #63 can be closed.
