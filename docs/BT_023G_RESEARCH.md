# BT-023G real data research run

## BT-023G

`ai_trainable_model.py` implements deterministic full-batch multinomial
logistic regression with SELL/HOLD/BUY labels. It is trainable and
explainable through its fixed features and coefficients; it is not a rules
engine. Features combine closed 1m, 5m and 15m OHLCV trend, volume,
volatility and momentum with the repository's multi-timeframe regime
classifier. Missing, stale, unclosed, misaligned or uncertain-regime input
fails as NO_TRADE. Artifacts and manifests are canonical JSON with SHA-256
checks; normalization is fitted only on the training partition and the split
enforces label maturity before test begins.

Prediction probabilities are explicitly uncalibrated. The model has no
database, exchange, account or order imports; a separate allowlisted research
store clocks predictions in PostgreSQL and keeps outcomes in an append-only
table. Its scheduler is OFF; collection and training require explicit CLI
commands. The service accepts only a local `trader_ai_research` DB with an
explicit research opt-in, or the named disposable integration test DB.

## Real data run

The collector fetched seven complete UTC days from the public Binance Spot
kline endpoint: 2026-10-15 through 2026-10-21 inclusive. It retained 10,080
1m, 2,016 5m, and 672 15m candles for each of BTCUSDT and ETHUSDT (25,536 total
candles). Each timeframe was checked for exact expected row count, alignment,
duplicates, gaps and closed-bar bounds. The source manifest records all six
file hashes and retrieval scope at
`research_artifacts/bt023g/manifest_1792022400000_1792627200000.json`.
`python scripts/bt023g_research.py collect --start-date 2026-10-15
--end-date-exclusive 2026-10-22` resumes from page checkpoints and stops on
HTTP 418/429. Training command:
`python scripts/bt023g_research.py train <manifest-path>`.

Labels are 15-minute-ahead close direction with a fixed ±5 bp flat band.
This is a classification target, not a trade return or fee-adjusted outcome.
The split is chronological 60/20/20 with 15-minute purging at each boundary;
the model fit uses only the first 60%. Three expanding walk-forward folds are
scored. Metrics below are the untouched test portions; Brier is the sum of
three class squared errors. The baseline class proportions come from training
data. No commission, spread, slippage or fills are assumed.

| Symbol | Train / calibration / test (UTC) | Test samples | Test Brier (baseline) | Log loss | Calibration split Brier / ECE | Test ECE | Brier gain vs baseline | Walk-forward gains |
|---|---|---:|---:|---:|---:|---:|---:|---|
| BTCUSDT | Oct 15 12:31–Oct 19 01:12 / Oct 19 01:52–Oct 20 13:16 / Oct 20 14:18–Oct 21 22:09 | 410 | 0.7467 (0.6769) | 1.1953 | 0.5950 / 0.0208 | 0.1954 | -0.0698 | -0.0642, +0.0833, -0.0107 |
| ETHUSDT | Oct 15 08:20–Oct 19 01:37 / Oct 19 02:08–Oct 20 13:43 / Oct 20 14:20–Oct 21 23:43 | 424 | 0.6503 (0.6510) | 1.0720 | 0.6103 / 0.0711 | 0.0631 | +0.0006 | -0.0278, -0.0015, -0.0273 |

Calibration/test class counts (SELL/HOLD/BUY): BTC 138/115/141 and
146/120/144; ETH 168/79/161 and 199/93/132, respectively.

Artifacts and the complete per-fold metrics are in
`research_artifacts/bt023g/`. This short, single-regime sample does not prove
calibration or stable predictive gain: BTC underperforms the baseline, ETH's
test improvement is negligible and all ETH walk-forward folds underperform.
Both artifacts remain `UNVERIFIED` and `RESEARCH_ONLY_NOT_APPROVED`.
