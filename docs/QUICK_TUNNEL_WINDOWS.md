# TradePilot BT-009C — Free, protected Quick Tunnel on your own Windows PC

**Goal:** first personal, browser-accessible PAPER **preview**, $0 additional
hosting/domain purchase, then later move the same isolated preview to an
approved cloud server. This is temporary development access **only**.
No real orders, Binance credentials, automatic scheduler, Cloudflare account
changes, DNS changes or paid resources.

Cloudflare announced email-PIN protected Quick Tunnels on **2026-10-02**:
https://developers.cloudflare.com/changelog/post/2026-10-02-protected-quick-tunnels/
Cloudflare's current Quick Tunnel documentation:
https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/

## Before you start

1. Have Docker Desktop **running**, with sufficient local RAM and disk.
2. Install Python 3 (used only to generate scrypt password hashes; raw
   passwords stay in your terminal), Git and current cloudflared:
   Windows PowerShell: \`winget install --id Cloudflare.cloudflared -e\`.
   Restart your terminal after installing cloudflared.
3. Use the TradePilot \`dev\` branch; do **not** run commands against Tam Kare.
   Pull the latest committed version of
   https://github.com/imhollyy/binance-ai-trader/tree/dev .
4. Decide which **single personal email** receives the Cloudflare one-time
   PIN. Do not use wildcard domains, production customer data or real
   Binance keys. The email is passed only to the cloudflared client
   argument and displayed locally. Neither the email nor password should
   be pasted in a GitHub issue, PR or chat.
5. Make sure localhost **port 8765** and private Compose subnet
   \`172.30.210.0/24\` are unused by any other service. This is a separate
   Compose project \`tradepilot_quick\`. It does not touch the normal
   local development \`compose.yaml\`.

## Start (from the project root)

In Windows PowerShell:

\`\`\`powershell
# Use your own allowed email address, do not paste it into this repository:
.\scripts\Start-TradePilotQuickPreview.ps1 -AllowedEmail "YOUR_EMAIL@example.com"
\`\`\`

If Windows blocks local scripts, run PowerShell once with the
\`-ExecutionPolicy Bypass -File\` option for this **specific** script after
reviewing it; do not globally disable Windows security settings.

The startup script strictly requires the Cloudflare \`--allowed-mail\` flag;
old cloudflared versions are rejected, not silently published. It starts
\`cloudflared tunnel --url http://127.0.0.1:8765 --allowed-mail YOUR_EMAIL\`
in a separately tracked process, extracts its new HTTPS
\`*.trycloudflare.com\` URL, and adjusts the **exact** frontend/API
hostnames to match that random URL. It asks for **two different**, new,
16–256 character staging viewer and operator passwords *only* on the
first run, storing scrypt hashes and a random, disposable local DB
credential in the ignored \`.env.preview.quick\`. Remember both passwords;
plaintext passwords are **not** saved.

Docker then builds and starts the private PostgreSQL, staging-only FastAPI,
staging Next.js viewer and a Caddy local HTTP origin **bound only to
127.0.0.1:8765**. Cloudflare terminates browser HTTPS; cloudflared carries
requests outbound through its protected tunnel to localhost HTTP.
Caddy strips spoofable forwarding headers and attests HTTPS only to the
privately networked FastAPI server. The frontend public origin, FastAPI
TrustedHost and exact Origin validation are all locked to the discovered
random host. The local Caddy origin is NOT safe to expose directly via a
LAN/port-forward/unrestricted tunnel. Only email-PIN-gated requests should
ever enter from outside.

When a "Private URL: https://...trycloudflare.com" message appears, open
that URL on your phone (mobile data is fine); enter your exact allowed
email; use the PIN Cloudflare emails you; and then sign in as **viewer**
with the preview-only password you chose. The displayed PAPER dashboard
must be *read only*. Local login uses a distinct Secure, HttpOnly,
SameSite=Strict cookie; logout/session expiry should require a new sign-in.

**The address changes at each restart**, and your PC/Docker must stay
running for you to use it. No guarantee of uptime, 7/24 worker, or fixed
domain. This setup does not require a Cloudflare account, even if you have
one connected to ChatGPT. The Cloudflare connected-app API failure does not
block the manually launched Quick Tunnel.

## Stop safely (preserve your PAPER data)

\`\`\`powershell
.\scripts\Stop-TradePilotQuickPreview.ps1
\`\`\`

The stop script terminates only the recorded cloudflared process and
brings down only the \`tradepilot_quick\` Compose project **without -v**.
A restart reuses the isolated database and hashed passwords but updates
the frontend/API hostname to the NEW short-lived URL. Never run
\`docker compose down -v\` or \`docker volume prune\` if you want to keep
its data.

Local-only process metadata/logs are ignored in \`.local-tradepilot/\`.
Do not share \`.env.preview.quick\`, local cloudflared logs, password
screenshots or the database password. For troubleshooting, use only
sanitized *status codes* and remove secrets before sharing logs.

## Acceptance before inviting additional testers

- [ ] Authorized viewer receives and completes the Cloudflare email PIN.
- [ ] Wrong/unlisted email cannot proceed past the Cloudflare gate.
- [ ] Mobile real browser (360/390px) can sign in via public HTTPS, see
      PAPER-only data, refresh, sign out, and is rejected on cookie replay.
- [ ] Viewer cannot mutate settings; no scheduler executes or reports
      itself running without proof.
- [ ] Computer off / tunnel stopped makes the external URL inaccessible.
- [ ] No Binance private keys, real orders or financial/customer data are
      imported to this disposable preview.

**CI:** The new localhost-only HTTP Caddy Quick Tunnel origin smoke
checks fixed Host rejection, trusted HTTPS scheme upstream, viewer auth
against synthetic PostgreSQL and internal health denial. It does NOT call
Cloudflare, exercise email PIN or test a real mobile browser. The
independent local TLS Compose smoke still tests Secure browser-cookie
settings. Record actual external email-PIN/mobile checks separately as
manual evidence; never report them as automated CI PASS.

**Later cloud move:** provision a separate approved always-on server,
encrypted backup and stable Access-protected domain, migrate the PAPER
database through verified backup/restore, and switch the tunnel.
Do not simply expose this HTTP loopback Caddy variant to a public IP.
