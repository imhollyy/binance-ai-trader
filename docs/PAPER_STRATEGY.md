# BT-005B — Causal PAPER Spot strategy signals

This is an offline, historical, PAPER-ONLY demonstration. There is no Binance private API, HTTP trade route, database access, worker, real trade, or production deployment.

## Feed and signal chronology

- Run the existing closed Spot feed quality check before ANY strategy callback: READY only, at least 50 closed, contiguous, correctly ordered candles, bounded to 1000 and checked against an explicit historical replay clock.
- Empty, stale, invalid, duplicate, gapped, still-open or wrong-market candle histories are rejected. Exchange source errors, including HTTP 418/429 when passed as source_error, are rejected; this module performs no network calls.
- Every eligible candle is visited in chronological order, after the fixed warmup. The strategy receives a frozen observation with an immutable tuple prefix ending at that candle and a hypothetical position-open flag.
- The last candle is never offered for a trade because it has no next open. Decisions must name the observed CLOSE timestamp, never a future timestamp.
- The immutable PAPER backtest models an assumed fill at the NEXT candle OPEN, with adverse fees and slippage. A gap against the stop is rejected, not silently repaired.
- Maximum 100 decisions. Invalid Decimal quantities/stops, unapproved synthetic approvals, duplicate buy while open, or exit while flat are rejected.

## Built-in strategy example

SmaCrossStrategy(SmaCrossParameters(fast_window=5, slow_window=20, quantity=Decimal('1'), stop_distance_pct=Decimal('1'))) is the only built-in example. A BUY signal is generated only on a confirmed fast-average cross above the slow average while flat. A SELL closes the hypothetical entire Spot position when the fast average crosses below the slow while holding. All averages use only observed CLOSE prices, including the prior window; no future candle is inspected.

Bounds: 2 <= fast_window < slow_window <= 49; positive finite Decimal quantity; 0 < stop_distance_pct <= 10. No parameter optimization, predictive model, guaranteed fill, real stop order, leverage, Futures, or performance forecast.

Usage: call run_causal_paper_spot_backtest(candles, symbol='BTCUSDT', interval='1m', as_of_ms=candles[-1].close_time_ms + 1, settings=RiskSettings(bot_enabled=True, manual_approval=False), strategy=SmaCrossStrategy()). The returned decisions and book are immutable hypothetical historical data. With an open position, cash is NOT total equity. There is no live price, unrealized PnL, or real Binance balance.

## Safety caveats and testing

Custom strategy implementations are UNSANDBOXED: a malicious callback could capture global/future data through its own closure. Only the built-in example is designed and regression-tested to be causal. Never claim all third-party strategies are future-proof.

Replay rejects the entire run on failed risk/preflight, approval, realized daily loss, or unsupported overnight position; it does NOT drop losing trades. Stop-loss is an assumption, not automatic execution or a maximum-loss guarantee.

New tests cover immutable prefixes, no last-bar evaluation, future-shock invariance, deterministic replay, fresh/invalid/stale/gap input, 418/429, duplicate entry, invalid signals, limit bounds, commissions/slippage, manual approval, daily losses and next-open gaps.

CI must validate the PR HEAD using Ruff, mypy, pytest, isolated PostgreSQL, Docker persistence and full-stack E2E. No customer data or existing PAPER account tables are modified. Automation, realistic fills, causal third-party sandboxing, model validation, strategy UX and deployment are separate future jobs.
