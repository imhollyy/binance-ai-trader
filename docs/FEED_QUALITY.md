# BT-004C — Public Spot data quality gate

A future paper strategy may consume **only** `FeedStatus.READY` data.

The pure function `feed_quality.evaluate_closed_spot_feed` checks:
- Allowed BTCUSDT/ETHUSDT 1m/5m/15m inputs
- Strict UTC millisecond ordering, aligned windows and closed-only candles
- OHLCV invariants, finite positive prices and nonnegative volume
- Missing intervals and stale latest closing time
- Bounded warm-up history before any indicator/strategy can run

Statuses `empty`, `insufficient_history`, `stale`, `gaps`, and `invalid`
must **not** trigger paper strategy evaluation. The gate is NOT yet connected
to a worker or strategy, so no live or simulated trading is triggered.

This gate does not promise market continuity, data provenance or price
integrity beyond its explicit checks. Future ingestion/strategy code must
integrate it, test stale connectivity and implement backfill before relying
on it. Futures and private exchange access remain outside scope.
