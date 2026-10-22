# ruff: noqa: E501
"""Bounded public Spot data collection and research-only model evaluation."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "research_artifacts" / "bt023g"
BASE = "https://data-api.binance.vision/api/v3/klines"
SYMBOLS = ("BTCUSDT", "ETHUSDT")
PERIODS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
MAX_DAYS = 30


def canonical(row: list[object], symbol: str, interval: str) -> list[str | int]:
    period = PERIODS[interval]
    if len(row) != 12 or type(row[0]) is not int or type(row[6]) is not int:
        raise ValueError("malformed_kline")
    opened, closed = row[0], row[6]
    if opened % period or closed != opened + period - 1:
        raise ValueError("invalid_candle_grid")
    values = [Decimal(str(row[index])) for index in (1, 2, 3, 4, 5)]
    op, high, low, close, volume = values
    if (
        min(op, high, low, close) <= 0
        or volume < 0
        or not low <= min(op, close) <= max(op, close) <= high
    ):
        raise ValueError("invalid_ohlcv")
    return [symbol, interval, opened, closed, *(format(v, "f") for v in values)]


def collect(
    days: int, start_date: str | None = None, end_date_exclusive: str | None = None
) -> Path:
    if start_date is not None or end_date_exclusive is not None:
        if start_date is None or end_date_exclusive is None:
            raise ValueError("both_start_and_end_date_required")
        start_dt = datetime.fromisoformat(start_date).replace(tzinfo=UTC)
        end_dt = datetime.fromisoformat(end_date_exclusive).replace(tzinfo=UTC)
        duration = (end_dt - start_dt).total_seconds()
        if duration <= 0 or duration % 86_400 or duration > MAX_DAYS * 86_400:
            raise ValueError(f"date_range_must_be_1_to_{MAX_DAYS}_whole_utc_days")
    else:
        if type(days) is not int or not 1 <= days <= MAX_DAYS:
            raise ValueError(f"days_must_be_1_to_{MAX_DAYS}")
        end_dt = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        start_dt = end_dt - timedelta(days=days)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    end_ms = int(end_dt.timestamp() * 1000)
    start_ms = int(start_dt.timestamp() * 1000)
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "scope": "public_spot_ohlcv_research_only",
        "source": BASE,
        "symbols": SYMBOLS,
        "intervals": tuple(PERIODS),
        "start_ms": start_ms,
        "end_exclusive_ms": end_ms,
        "days": (end_ms - start_ms) // 86_400_000,
        "retrieved_at_utc": datetime.now(UTC).isoformat(),
        "files": [],
    }
    files: list[dict[str, object]] = []
    with httpx.Client(timeout=20, follow_redirects=False, trust_env=False) as client:
        for symbol in SYMBOLS:
            for interval, period in PERIODS.items():
                path = ARTIFACTS / f"{symbol}_{interval}_{start_ms}_{end_ms}.json"
                if path.exists():
                    raw = path.read_bytes()
                    rows = json.loads(raw)
                else:
                    partial = path.with_suffix(".partial.json")
                    rows = (
                        json.loads(partial.read_text(encoding="utf-8")) if partial.exists() else []
                    )
                    for i, row in enumerate(rows):
                        if row[2] != start_ms + i * period:
                            raise ValueError(f"invalid_partial_checkpoint_{symbol}_{interval}")
                    cursor = start_ms + len(rows) * period
                    while cursor < end_ms:
                        response = client.get(
                            BASE,
                            params={
                                "symbol": symbol,
                                "interval": interval,
                                "startTime": cursor,
                                "endTime": end_ms - 1,
                                "limit": 1000,
                            },
                        )
                        if response.status_code in (418, 429):
                            raise RuntimeError(
                                f"exchange_restriction_http_{response.status_code}; stop collection"
                            )
                        response.raise_for_status()
                        batch = response.json()
                        if not isinstance(batch, list) or not batch:
                            break
                        converted = [canonical(item, symbol, interval) for item in batch]
                        if converted[0][2] != cursor:
                            raise ValueError(f"gap_or_duplicate_{symbol}_{interval}_{cursor}")
                        rows.extend(converted)
                        cursor = int(converted[-1][2]) + period
                        partial.write_text(
                            json.dumps(rows, ensure_ascii=True, separators=(",", ":")),
                            encoding="utf-8",
                        )
                        time.sleep(0.12)
                    if not rows:
                        raise ValueError(f"empty_public_data_{symbol}_{interval}")
                    for i, row in enumerate(rows):
                        if row[2] != start_ms + i * period:
                            raise ValueError(f"gap_or_duplicate_{symbol}_{interval}_{row[2]}")
                    payload = json.dumps(rows, ensure_ascii=True, separators=(",", ":")).encode()
                    temporary = path.with_suffix(".json.tmp")
                    temporary.write_bytes(payload)
                    temporary.replace(path)
                    partial.unlink(missing_ok=True)
                    raw = payload
                files.append(
                    {
                        "path": path.name,
                        "symbol": symbol,
                        "interval": interval,
                        "count": len(rows),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "first_open_ms": rows[0][2],
                        "last_open_ms": rows[-1][2],
                    }
                )
    manifest["files"] = files
    manifest_path = ARTIFACTS / f"manifest_{start_ms}_{end_ms}.json"
    manifest_temp = manifest_path.with_suffix(".json.tmp")
    manifest_temp.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_temp.replace(manifest_path)
    return manifest_path


def _candle(row: list[object]):
    from binance_ai_trader.market_data import Candle

    return Candle(
        str(row[0]), str(row[1]), int(row[2]), int(row[3]), *(Decimal(str(v)) for v in row[4:9])
    )


def _load_manifest(path: Path):
    manifest = json.loads(path.read_text(encoding="utf-8"))
    datasets: dict[tuple[str, str], tuple] = {}
    for entry in manifest["files"]:
        data_path = path.parent / entry["path"]
        raw = data_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"artifact_hash_mismatch:{entry['path']}")
        records = json.loads(raw)
        candles = tuple(_candle(row) for row in records)
        period = PERIODS[entry["interval"]]
        if len(candles) != entry["count"] or any(
            c.open_time_ms != candles[i - 1].open_time_ms + period
            for i, c in enumerate(candles)
            if i
        ):
            raise ValueError(f"gap_or_duplicate:{entry['path']}")
        datasets[entry["symbol"], entry["interval"]] = candles
    for symbol in SYMBOLS:
        for interval, period in PERIODS.items():
            candles = datasets[symbol, interval]
            if (
                candles[0].open_time_ms != manifest["start_ms"]
                or candles[-1].open_time_ms + period != manifest["end_exclusive_ms"]
                or len(candles) != (manifest["end_exclusive_ms"] - manifest["start_ms"]) // period
            ):
                raise ValueError(f"cross_timeframe_alignment_failure:{symbol}:{interval}")
    return manifest, datasets


def _rows_for_symbol(datasets, symbol: str):
    from binance_ai_trader.ai_market_regime import TimeframeHistory
    from binance_ai_trader.ai_trainable_model import TrainingRow, extract_features

    frames = {interval: datasets[symbol, interval] for interval in PERIODS}
    one = frames["1m"]
    rows = []
    for i in range(35, len(one) - 16):
        current = one[i]
        now = current.close_time_ms + 1
        histories = []
        for interval in PERIODS:
            closed = tuple(c for c in frames[interval] if c.close_time_ms < now)
            if len(closed) < 30:
                break
            histories.append(TimeframeHistory(interval, closed[-30:]))
        if len(histories) != 3:
            continue
        try:
            features = extract_features(tuple(histories), symbol=symbol, now_ms=now)
        except ValueError:
            continue
        future = one[i + 15]
        move_bps = (future.close / current.close - 1) * Decimal(10_000)
        label = "BUY" if move_bps > 5 else "SELL" if move_bps < -5 else "HOLD"
        rows.append(TrainingRow(current.close_time_ms, features, label, future.close_time_ms))
    return tuple(rows)


def _probabilities(artifact_json: str, features: tuple[float, ...]) -> list[float]:
    from binance_ai_trader.ai_trainable_model import _softmax

    artifact = json.loads(artifact_json)
    x = [1.0] + [
        (v - m) / s for v, m, s in zip(features, artifact["means"], artifact["scales"], strict=True)
    ]
    return _softmax([sum(a * b for a, b in zip(w, x, strict=True)) for w in artifact["weights"]])


def _metrics(probabilities, labels, baseline_labels) -> dict[str, object]:
    classes = ("SELL", "HOLD", "BUY")
    counts = Counter(labels)
    brier = sum(
        sum((p[j] - int(classes[j] == y)) ** 2 for j in range(3))
        for p, y in zip(probabilities, labels, strict=True)
    ) / len(labels)
    loss = -sum(
        math.log(max(p[classes.index(y)], 1e-15))
        for p, y in zip(probabilities, labels, strict=True)
    ) / len(labels)
    bins: dict[int, list[tuple[float, int]]] = {}
    for p, y in zip(probabilities, labels, strict=True):
        k = max(range(3), key=p.__getitem__)
        bins.setdefault(min(int(p[k] * 10), 9), []).append((p[k], int(classes[k] == y)))
    ece = sum(
        len(g) / len(labels) * abs(sum(x for x, _ in g) / len(g) - sum(y for _, y in g) / len(g))
        for g in bins.values()
    )
    baseline_counts = Counter(baseline_labels)
    baseline = sum(
        sum((baseline_counts[c] / len(baseline_labels) - int(c == y)) ** 2 for c in classes)
        for y in labels
    ) / len(labels)
    return {
        "samples": len(labels),
        "class_counts": dict(counts),
        "brier": brier,
        "log_loss": loss,
        "top_label_ece": ece,
        "prevalence_baseline_brier": baseline,
        "brier_improvement_vs_baseline": baseline - brier,
    }


def train(path: Path) -> Path:
    from binance_ai_trader.ai_trainable_model import train_chronological

    source_manifest, datasets = _load_manifest(path)
    reports = {}
    for symbol in SYMBOLS:
        rows = _rows_for_symbol(datasets, symbol)
        if len(rows) < 1500:
            raise ValueError(f"insufficient_real_rows:{symbol}:{len(rows)}")
        # 60/20/20 chronological train/calibration/test, with 15-minute label embargo.
        a, b = int(len(rows) * 0.6), int(len(rows) * 0.8)
        # Purge 15 one-minute observations at each boundary for the 15m target.
        train_rows = rows[: max(0, a - 16)]
        calibration = rows[a + 15 : max(a + 15, b - 16)]
        test = rows[b + 15 :]
        if (
            train_rows[-1].outcome_available_at_ms > calibration[0].close_at_ms
            or calibration[-1].outcome_available_at_ms > test[0].close_at_ms
        ):
            raise ValueError("chronological_embargo_failed")
        fitted = train_chronological(
            tuple(rows[:b]),
            model_version=f"bt023g-logreg-v1-{symbol}",
            test_fraction=0.25,
            outcome_horizon_ms=900_000,
        )
        # train_chronological fits only its prefix; embargo leaves calibration tail unused by fit.
        artifact = json.loads(fitted.artifact_json)
        fit_manifest_path = ARTIFACTS / f"fit_manifest_{symbol}_v1.json"
        fit_manifest_path.write_text(fitted.manifest_json + "\n", encoding="utf-8")
        if fitted.train_end_ms >= calibration[0].close_at_ms:
            raise ValueError("calibration_overlap")
        val_probs = [_probabilities(fitted.artifact_json, row.features) for row in calibration]
        test_probs = [_probabilities(fitted.artifact_json, row.features) for row in test]
        # Multiple forward folds: each fold's model sees only an earlier expanding window.
        folds = []
        for fraction in (0.55, 0.65, 0.75):
            cutoff = int(len(rows) * fraction)
            end = min(len(rows), cutoff + int(len(rows) * 0.08))
            fold_model = train_chronological(
                tuple(rows[:cutoff]),
                model_version=f"bt023g-wf-{symbol}-{fraction}",
                test_fraction=0.2,
                outcome_horizon_ms=900_000,
            )
            fold_test = rows[end - int(len(rows) * 0.08) : end]
            fold_probs = [
                _probabilities(fold_model.artifact_json, row.features) for row in fold_test
            ]
            fold_baseline = rows[: int(cutoff * 0.8)]
            folds.append(
                {
                    "start_ms": fold_test[0].close_at_ms,
                    "end_ms": fold_test[-1].close_at_ms,
                    **_metrics(
                        fold_probs, [r.label for r in fold_test], [r.label for r in fold_baseline]
                    ),
                }
            )
        artifact_path = ARTIFACTS / f"model_{symbol}_v1.json"
        artifact_bytes = fitted.artifact_json.encode()
        artifact_path.write_bytes(artifact_bytes)
        reports[symbol] = {
            "rows": len(rows),
            "period_start_ms": rows[0].close_at_ms,
            "period_end_ms": rows[-1].close_at_ms,
            "train_period": [rows[0].close_at_ms, rows[a - 1].close_at_ms],
            "calibration_period": [calibration[0].close_at_ms, calibration[-1].close_at_ms],
            "test_period": [test[0].close_at_ms, test[-1].close_at_ms],
            "train_class_counts": dict(Counter(r.label for r in train_rows)),
            "calibration": _metrics(
                val_probs, [r.label for r in calibration], [r.label for r in train_rows]
            ),
            "test": _metrics(test_probs, [r.label for r in test], [r.label for r in train_rows]),
            "walk_forward": folds,
            "artifact": artifact_path.name,
            "training_manifest": fit_manifest_path.name,
            "training_manifest_sha256": hashlib.sha256(fit_manifest_path.read_bytes()).hexdigest(),
            "artifact_sha256": hashlib.sha256(artifact_bytes).hexdigest(),
            "feature_schema": artifact["features"],
            "calibration_status": "UNVERIFIED",
            "approval_status": "RESEARCH_ONLY_NOT_APPROVED",
            "label_definition": "15m ahead close direction; +/-5 bps flat band; not net trade return",
        }
    report = {
        "schema_version": "1.0",
        "source_manifest": path.name,
        "source_manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "symbols": reports,
        "trade_costs": "NOT_INCLUDED; no fill profitability claim",
    }
    output = ARTIFACTS / "training_walk_forward_report.json"
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    collect_parser = sub.add_parser("collect", help="bounded public Spot candle collection")
    collect_parser.add_argument("--days", type=int, default=7)
    collect_parser.add_argument("--start-date", type=str)
    collect_parser.add_argument("--end-date-exclusive", type=str)
    train_parser = sub.add_parser("train", help="train and score against a retained manifest")
    train_parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        output = (
            collect(args.days, args.start_date, args.end_date_exclusive)
            if args.command == "collect"
            else train(args.manifest.resolve())
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print(f"research_failed:{exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
