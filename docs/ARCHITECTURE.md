# Architecture — incremental delivery

## BT-001 (implemented)

- Python 3.12 package in `src/binance_ai_trader`
- Paper-only, immutable runtime configuration
- Offline CLI for quick boot and fail-closed checks
- Unit-test and static-analysis quality gates
- GitHub Actions with repository read-only permission

**Nothing here contacts Binance or places any trade.**

## BT-002 and later (planned, not implemented)

```text
Binance public REST / market WebSocket
      |
      v
Market-data adapter --> validation --> candles storage (PostgreSQL)
                                           |
                                           v
                                      indicators
                                           |
                                           v
                                   backtest / paper engine
                                           |
                                           v
                                     risk engine
                                           |
                                           v
                                   paper-order journal
                                           |
                                           v
                                      read-only API
                                           |
                                           v
                                        dashboard
```

A separate authenticated live-order adapter is intentionally excluded from early packages. No AI response, signal or UI component can bypass a deterministic risk gate in the future design.

## Engineering constraints

- UTC timestamps and fixed-point/Decimal calculations for monetary data.
- Idempotent candle writes (symbol, interval, opening timestamp).
- Detect missing candles and recover them via REST after stream disconnections.
- Respect exchange request limits and exponential backoff.
- Stable strategy/version metadata for reproducible backtests.
- No look-ahead bias; include fees, slippage and spread in evaluation.
- Trades/orders, when eventually supported, require unique client IDs, status reconciliation and audit events.
- Default to stopping new actions on stale data, exchange uncertainty or limit breaches.

The architecture is aspirational; none of these later components is claimed complete in BT-001.

## BT-003A public Spot adapter

`market_data.PublicSpotMarketClient` contacts only
`https://data-api.binance.vision/api/v3/klines` with public HTTP GET,
no account/API key or order endpoints. BTCUSDT and ETHUSDT, intervals
1m, 5m, 15m and bounded limits only. Returned prices are precise
`Decimal` values; malformed, unsorted or unfinished candles are not
accepted as finished market history. HTTP 418/429 returns an explicit
rate-limit exception and is **never retried automatically**.

This first client is implemented and offline-tested but is not yet
wired to a background worker, API or database. Later package BT-003B
adds durable candle storage, gap recovery and bounded periodic ingestion.
