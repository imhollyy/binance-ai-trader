# Security model (BT-001)

## Scope and current protection

This codebase is **research only**. It contains no authenticated Binance client, API-key configuration, wallet access, order endpoint, futures support, or ability to trade. `TRADING_MODE` must resolve to `paper`; all other values are rejected.

- Never commit or paste exchange API keys, secret keys, wallet credentials, recovery codes or MFA codes.
- No order creation, withdrawal or transfer API permissions are required for BT-001.
- Secrets must never be added to GitHub repository files, issue comments, logs, CI output or screenshots.
- Keep this repository private and give collaborators least-privilege access.
- Review dependency and CI permissions before accepting changes.
- Treat external market data as untrusted: validate symbols, timestamps, response sizes and numeric bounds in future packages.

## Planned before any authenticated API connection

1. Exchange keys are generated with only the minimum required permissions.
2. Withdrawals/transfers remain disabled; static IP allowlisting is required where supported.
3. Keys live in a managed secret store, not source files or local plaintext committed to Git.
4. Separate low-balance account/subaccount and a manual kill switch are configured.
5. Risk and order-management engine passes simulation, reconciliation and failure-injection tests.
6. Real-money capabilities need a **separate explicit approval**, implementation PR and deployment gate.

If a credential is disclosed, revoke it with the exchange and rotate it immediately.
