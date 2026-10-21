# BT-009C — First accessible PAPER beta: local stack assembled, NOT deployed

**Status:** local-only Docker stack + CI fixture. This is the first coherent
Next.js + FastAPI + PostgreSQL + HTTPS reverse-proxy **assembly**, not a
working public URL or Cloudflare Access deployment. No provider resources,
DNS, paid hosting, production data, real Binance orders or scheduler start.

## What is now runnable in an isolated test environment

The explicit, independent \`compose.preview.yaml\` provides:

- \`preview-db\`: its **own** \`trader_preview\` PostgreSQL DB and isolated
  named volume, not the usual \`compose.yaml\` PAPER development DB.
- \`preview-api\`: staging \`API_AUTH_MODE=staging\` (never \`ADMIN_TOKEN\`),
  preflight required *before* Alembic migrates; PostgreSQL service host,
  database, role, PAPER mode, no scheduler, and HTTPS shared host checked
  fail-closed. Uvicorn raw access log disabled; only proxy IP
  \`172.30.210.10\` is trusted for forwarded scheme.
- \`preview-web\`: a separate Next.js production image built with
  \`NEXT_PUBLIC_TP_STAGING_PREVIEW=enabled\` and the **one shared** HTTPS
  origin; viewer interface is read-only.
- \`preview-ingress\`: Caddy internal-CA TLS, same-host routing:
  \`/api/v1/*\` → API, other public pages → web, internal health/docs
  denied. Client Forwarded headers stripped and replaced. No Caddy
  access logging configured. Network/API/DB are Docker-private; the only
  *host* published port is **127.0.0.1:443**, not a public interface.

\`compose.yaml\` and its local \`X-Admin-Token\` dev stack remain untouched.
\`PRIVATE_PREVIEW_OPT_IN=isolated-paper-only\` and all staging-only config are
required. All four preview services have the explicit \`preview\` profile,
so a normal local \`docker compose up\` cannot activate this stack.

## Local CI proof (no provider, synthetic accounts)

GitHub Actions \`Private Preview Local HTTPS Smoke\` uses a unique Compose
project name and generates an **untracked synthetic CI-only** env file; no
real passwords or keys are committed or displayed. It builds the web image,
migrates an empty disposable DB, serves a local *internally signed* HTTPS
endpoint with the illustrative hostname \`preview.staging.example\`, and
tests real HTTPS socket routing and login → Secure cookie → authorized
PAPER dashboard → viewer write denial → logout/revoked cookie. It checks
wrong Origin and rejects health paths at the ingress. It destroys only
its own disposable Compose volumes and generated fixture on exit.

**Trust boundary:** CI \`curl --insecure --resolve\` exists solely because
Caddy's local throwaway CA is not a publicly trusted certificate. This
is NOT proof of production TLS trust, browser cookie SameSite, Access MFA,
Cloudflare edge/WAF, offsite backup, hosted DB, real browser login or
24/7 uptime. DO NOT copy \`--insecure\` into a real staging launch.

## Before a real private beta URL (requires explicit separate authorization)

1. **Owner decision:** specify the real domain and authorized preview
   tester email(s), Cloudflare account/zone, hosting/DB provider and
   acceptable recurring cost. Until then no provider/API/DNS changes.
2. Decide the Cloudflare Access + MFA/WAF/rate-limit policy and trusted
   TLS/cert and Tunnel→origin configuration. The local internal CA is
   **not** the production certificate; audit Caddy/Cloudflare/Next
   request logs so cookies/URL tokens/passwords never reach logs.
3. Create an independent encrypted staging-only PostgreSQL instance and
   secret store, backups with retention and restore drill. No personal,
   production, or prior PAPER account data copying without consent.
4. Review host binding, origin lockout, proxy-header provenance and direct
   backend bypass. Only the private ingress should reach the API;
   do not expose an \`ADMIN_TOKEN\` host or default Compose stack.
5. Real Chromium browser on 360, 390, 768, 1280px: signed-in
   cookie/CSRF/logout/replay, Access login and MFA, direct-origin denial,
   incorrect Host/Origin/Forwarded headers. Use synthetic PAPER fixtures.
6. Review uptime/rollback, infrastructure logs, password rotation with
   all old API instances drained, and backups **before** granting access.
   No default scheduler activation or real exchange trading.

This package **reduces the missing deployment assembly to provider
configuration and final acceptance**, but it does not authorize the
actual external staging deployment. The user's first URL and login can
be delivered only *after* that separately authorized setup is complete.

## Developer isolation and rollback

This stack requires an intentionally generated, untracked env file.
Never commit it, send it in issues, print it to stdout or pass real
secrets as command-line arguments. \`.dockerignore\` excludes secrets
from Docker contexts.

When running a disposable **local-only** stack under a distinct Compose
project name, \`docker compose -p <distinct-preview-project> --env-file
<untracked-preview-env> -f compose.preview.yaml --profile preview up
--build -d\` is the applicable shape. Do NOT run it on a machine already
serving important systems without reviewing port 443 and bridge subnet
conflicts.

Use the same selected project name/config/env with \`down\` to stop.
**\`down -v\` deletes that preview's persisted DB** and is allowed
only in the explicitly disposable CI fixture, never user staging or
production. The default-off PAPER worker is deliberately absent.

See also [Issue #37](https://github.com/imhollyy/binance-ai-trader/issues/37)
and [Issue #63](https://github.com/imhollyy/binance-ai-trader/issues/63).
