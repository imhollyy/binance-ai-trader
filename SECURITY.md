# Security model (BT-001)

## Public repository notice

**This repository is PUBLIC.** Its code, history, workflow logs, pull requests and issues must be considered publicly accessible. Never commit, print or post Binance API keys, secret keys, wallet credentials, recovery codes, MFA codes, private account balances, identifiable trade histories or proprietary strategies. Removing a leaked credential from a later commit does not undo its exposure; immediately revoke and rotate it.

## Scope and current protection

This codebase is **research only**. It contains no authenticated Binance client, API-key configuration, wallet access, order endpoint, futures support, or ability to trade. `TRADING_MODE` must resolve to `paper`; all other values are rejected.

- No order creation, withdrawal or transfer API permissions are required for BT-001.
- Keep all real secrets and private trading logic outside this public repository and out of CI outputs.
- Grant collaborators least-privilege access; review PRs, dependency scripts and CI permissions.
- Never allow untrusted pull-request code to access production keys or runtime environments.
- Treat external market data as untrusted: validate symbols, timestamps, response sizes and numeric bounds in future packages.

## Planned before any authenticated API connection

1. Exchange keys generated with the minimum necessary permissions.
2. Withdrawals/transfers remain disabled; static IP allowlisting where supported.
3. Keys in a managed secret store for a separate restricted deployment, not in public repo sources or logs.
4. Isolated, low-balance account/subaccount plus a manual kill switch.
5. Risk engine, reconciliation and failure-injection tests pass.
6. Real-money features require **separate explicit approval**, dedicated implementation PR, deployment gate and review of whether private source code is needed.

If a credential is disclosed, revoke it at Binance and rotate it immediately.
