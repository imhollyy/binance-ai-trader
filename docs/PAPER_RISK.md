# BT-004A — Deterministic paper-risk preflight

`paper_risk.evaluate_paper_risk` is a pure function. It receives validated
`RiskSettings`, a proposed *hypothetical* notional/stop-distance/leverage and
an externally provided simulated equity/capital/position snapshot.

It makes **no network calls**, does not read or write the database, cannot
place a real or paper order and never adjusts the simulated account itself.

Guardrails:
- No checks run as eligible while the paper bot is disabled.
- Spot leverages only 1×; Futures 1–5× applies to simulated margin only.
- Risk per hypothetical trade uses the configured market budget, not a real balance.
- Daily equity decline and peak-equity drawdown deny new proposals at the limit.
- No more than the configured number of simulated open positions.
- Requested margin or cash must fit remaining separately allocated capital.
- A mandatory numerical stop distance is used for a **loss estimate** only.
- Manual approval produces `approval_required`, never an implicit trade.

**Limits:** A stop-loss estimate is not guaranteed realized loss; gaps,
slippage, fees, funding, liquidation, disconnected markets and execution
uncertainty can cause greater losses. The risk engine must be extended,
reconciled and tested independently before any possible live execution.
There is no exchange/order adapter in this milestone.
