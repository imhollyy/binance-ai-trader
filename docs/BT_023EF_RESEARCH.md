# BT-023E/F forecast validation and offline research

## BT-023E: PostgreSQL outcome evaluation

The evaluator in ai_research_forecast_evaluator_test.py is deliberately
restricted to a local PostgreSQL database named trader_ai_forecast_test with
AI_FORECAST_TEST_ISOLATED=1. It is not wired to an application route, worker,
PAPER account, or exchange order path.

The evaluator uses PostgreSQL clock_timestamp(), fetches the same bounded
closed-candle interval from Binance's public Spot endpoint, and requires an
exact match with every persisted OHLCV row from the forecast observation
through its outcome due time. It checks a complete OHLCV grid and closed
candle timestamps. Missing, malformed, unfinished, mismatched, 418, or 429
source responses produce no result. It records the source URL, canonical
source-candle SHA-256, and retrieval time. The fixed classification band is
10 bps; callers cannot choose a label threshold or evaluation clock.

The outcome table is separate from receipts, unique by receipt, and rejects
UPDATE, DELETE, and TRUNCATE. PostgreSQL validates inserts against the receipt,
all intervening closed candles, exact source/target closes, expected label,
fixed band, and database time. The 0010 and 0011 downgrade guards refuse to
weaken historical result integrity. 0009 refuses to drop nonempty outcomes.
Receipt/account/journal tables are not cleared by evaluator code.

These controls establish transactional consistency inside the disposable
database. Binance does not digitally sign the public HTTPS response, so the
recorded digest proves byte identity while TLS and the endpoint identify the
retrieval path; it is not independent publisher attestation. The outcome and
all source candles in its horizon are protected after evaluation. A database
owner who can replace migrations/triggers remains outside this threat boundary.

## BT-023F: data and analysis limits

ai_walk_forward_research.py provides:

- A bounded historical fetch through the existing public-only Binance Spot
  client; it checks closed candles, exact interval continuity, HTTP 418/429
  handling, maximum window size, and records a canonical SHA-256.
- Expanding chronological train/calibration/validation/test windows with
  embargoes, independent fold reports, duplicate-ID/model-version checks,
  label maturity and overlap checks, class counts, Brier score, multiclass log
  loss, top-label reliability bins, and a calibration-only climatology
  baseline.
- Decimal close-to-close long replay that itemizes buy fee, sell fee, spread,
  slippage, total costs, net returns, compounded drawdown, wins/losses,
  heuristic prior-bar regimes, and cost stress multipliers.

No calibrator or model trainer is implemented. Walk-forward evaluation accepts
already-recorded predictions; it never manufactures historical forecasts.
Synthetic data in tests only checks math and fail-closed paths.

## Captured public Spot candles

research_artifacts/bt023f_binance_spot_manifest.json and its four raw JSON
files contain 288 BTCUSDT 5m candles per UTC day from the Binance public market
data endpoint. These are four isolated data windows, not an AI experiment:
Binance documents the public no-auth market-data host and Spot kline endpoint
[here](https://github.com/binance/binance-spot-api-docs/blob/master/faqs/market_data_only.md)
and [here](https://github.com/binance/binance-spot-api-docs/blob/master/rest-api.md#klinecandlestick-data).

| UTC day | Candles | Candle SHA-256 |
|---|---:|---|
| 2024-03-15 | 288 | e325617b35bb052539fdb2e105b4bb608a9d0f4891d2bebc8956d255256c5e25 |
| 2024-08-15 | 288 | 0696c0674d1258253ee9a6dee12a04571a4e31eec2fd54fe2c8e1ad1baeb2470 |
| 2025-04-15 | 288 | 17a80d2b694df924b10f46bf1f9becd884e57fb2d17d915facf919561f4f7380 |
| 2025-10-15 | 288 | 120010e3b18086c838d60a7fce8a83deb7e346118bc61de34ae439ec9e4ab2ba |

The manifest records retrieval timestamps, exact millisecond ranges, artifact
hashes, and descriptive rolling 20-bar trend buckets. Binance's public HTTPS
response is not signed; SHA-256 proves the retained bytes match the recorded
digest, not publisher identity, actual spreads, fees, or executable fills.
The observed close changes are descriptive data summaries only.

## Evidence verdict

**BT-023F model calibration and net strategy performance: BLOCKED /
INSUFFICIENT_EVIDENCE.** This repository has no AI model artifact and no
historically timestamped forecasts over these market windows. Existing test
forecasts are synthetic. Therefore no Brier/log-loss result on the captured
market data, positive expected value, predictive gain, or profitable
cost-adjusted strategy can be claimed. Cost inputs in the replay API are
explicit assumptions; they are not verified account fees, contemporaneous
order-book spreads, slippage observations, or actual fills.

Progressing beyond this status requires an identified model artifact and
prospectively saved forecasts before their target outcomes, with an independent
review of data/forecast provenance. No model activation, PAPER worker, real
orders, Futures execution, or live account access is part of BT-023E/F.
