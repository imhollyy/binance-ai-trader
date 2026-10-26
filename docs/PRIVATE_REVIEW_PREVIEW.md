# Isolated owner review preview

This is a separate local PAPER viewer. It does not reuse the protected
`tradepilot_quick` Compose project, database, network, port, environment file,
or named volumes.

## Isolation

- Compose project: `tradepilot_bt027_review`
- PAPER database: project-scoped `preview-db` / `trader_preview`
- Named volumes: project-scoped; the stop script never removes volumes
- Loopback ingress: `127.0.0.1:8877`
- Private network: `172.30.220.0/24`, checked against Docker networks and host
  routes before startup
- Viewer-only staging UI/API; no admin token, scheduler, strategy worker,
  exchange credentials, or Futures service
- No independent RiskGuard store is wired to this preview. Its real profile
  endpoint remains unavailable and risk selection is disabled. The isolation
  allowlist in `risk_store` and `api.py` is unchanged.

## Owner launch after explicit tunnel approval

Prerequisites: Docker Desktop, Python 3, current `cloudflared` with
`--allowed-mail`, and `curl.exe`. If cloudflared is missing, install it locally
with `winget install --id Cloudflare.cloudflared -e` after approving the new
external preview. The launcher checks that TCP 8877, the chosen
subnet, Compose project and project-scoped volumes are unused. It refuses to
reuse an existing review project automatically.

From the repository root, after approving a new external email-PIN tunnel, run
in a local PowerShell window:

```powershell
.\scripts\Start-TradePilotReviewPreview.ps1
```

The allowed email is entered in a hidden local prompt. The viewer password is
entered twice through Python's hidden password prompt. Only scrypt verifiers
and a random disposable database credential are written to the ignored
`.env.preview.review`, whose ACL is restricted to the current Windows user.
The short-lived URL and cloudflared logs stay in the ignored
`.local-tradepilot-review/` directory. Neither value is written to source or
GitHub.

The launcher starts only its email-PIN-protected tunnel and separate Compose
project. It checks exact-host local readiness, then certificate-validates an
external HTTPS response and confirms the Cloudflare edge. It prints the URL
only after those checks pass. Complete the email PIN, sign in as viewer, and
sign out after acceptance. Never use the unreachable operator role.

Stop only the isolated review services and tunnel with:

```powershell
.\scripts\Stop-TradePilotReviewPreview.ps1
```

This uses Compose `stop`; it does not remove the review database volume. Do not
run `down -v` or volume prune. It does not target `tradepilot_quick`.

## Browser acceptance

The review API must trust **only** its Docker ingress `172.30.220.10` for
`X-Forwarded-Proto: https`; the normal quick-preview ingress is
`172.30.210.10` and must not be trusted in this isolated review network.
A successful local HTTP 200 page response does **not** prove the HTTPS-only
viewer login path works. Browser PIN/password acceptance still requires the
owner's manual check after deploying this reviewed commit.

1. Confirm the empty account is presented as empty/unavailable, not fabricated
   capital or P&L.
2. Check recorded candle timestamps and that historical data is not labeled
   live.
3. Confirm RiskGuard is unavailable/disabled rather than showing invented
   risk levels.
4. Confirm any AI data is `RESEARCH ONLY — NOT APPROVED`; absent data and
   performance metrics remain absent.
5. Confirm worker status is not `RUNNING` without verified process evidence;
   no START control is present.
6. Check unknown spread/cost fields stay unknown and gross/net P&L stay
   separate.
7. Confirm the URL has no credentials, browser storage has no session secret,
   and logout ends the viewer session.

Production PAPER BUY, scheduler/strategy execution, Binance private orders,
Futures and unapproved model activation remain disabled.
