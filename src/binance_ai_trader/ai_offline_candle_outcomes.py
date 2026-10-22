"""BT-023C: OFFLINE candle outcome labeling with replayable hash-linked records.

Only caller-supplied closed Spot candles and self-reported recorded_at_ms values
are inspected. No trusted market-data attestation, actual fills, exchange fees,
model inference, persistence, API endpoint or order authorization is provided.
A hash chain without an independently trusted anchor is NOT an append-only DB.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from binance_ai_trader.ai_constitution import ProbabilityEstimate
from binance_ai_trader.ai_offline_model_review import ForecastOutcome, ObservedOutcome
from binance_ai_trader.market_data import INTERVAL_MS, SUPPORTED_SYMBOLS, Candle

GENESIS = "0" * 64
DOMAIN = b"tradepilot-offline-candle-labels-v1\n"
MAX_CANDLES = 20_000
MAX_FORECASTS = 2_000
MAX_COST_BPS = Decimal("1000")
MAX_FLAT_BPS = Decimal("200")
MAX_HORIZON_MS = 86_400_000
ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10_000)


class LabelStatus(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    INVALID_CANDLES = "INVALID_CANDLES"
    INVALID_FORECAST = "INVALID_FORECAST"
    MISSING_FUTURE_CANDLE = "MISSING_FUTURE_CANDLE"
    FUTURE_INFORMATION_RISK = "FUTURE_INFORMATION_RISK"
    UNANCHORED_RESEARCH_ONLY = "UNANCHORED_RESEARCH_ONLY"
    ALTERED_RESEARCH_CHAIN = "ALTERED_RESEARCH_CHAIN"
    DETACHED_REFERENCE_MISMATCH = "DETACHED_REFERENCE_MISMATCH"
    SELF_REPORTED_REPLAY_ONLY = "SELF_REPORTED_REPLAY_ONLY"


@dataclass(frozen=True, slots=True)
class RecordedForecast:
    signal_id: str
    model_version: str
    symbol: str
    interval: str
    observed_close_at_ms: int
    recorded_at_ms: int
    forecast: ProbabilityEstimate


@dataclass(frozen=True, slots=True)
class CandleLabelEntry:
    previous_hash: str
    entry_hash: str
    forecast_outcome: ForecastOutcome
    recorded_at_ms: int
    entry_close: Decimal
    target_close: Decimal
    hypothetical_roundtrip_cost_bps: Decimal
    hypothetical_net_return_bps: Decimal


@dataclass(frozen=True, slots=True)
class OfflineCandleLabelLedger:
    status: LabelStatus
    symbol: str | None = None
    candle_sha256: str | None = None
    head_sha256: str | None = None
    entries: tuple[CandleLabelEntry, ...] = ()

    @property
    def trade_authorized(self) -> bool:
        return False

    @property
    def qualifies_as_calibrated_model(self) -> bool:
        return False

    @property
    def no_trade(self) -> bool:
        return True


def _dec(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _hash(payload: object, *, previous: str = GENESIS) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("ascii")
    return hashlib.sha256(DOMAIN + previous.encode("ascii") + b"\n" + encoded).hexdigest()


def _price(number: object, *, allow_zero: bool = False) -> bool:
    return (
        type(number) is Decimal
        and number.is_finite()
        and (number >= ZERO if allow_zero else number > ZERO)
    )


def _valid_candle(item: object, symbol: str, interval: str, period: int) -> bool:
    if type(item) is not Candle or item.symbol != symbol or item.interval != interval:
        return False
    if (
        type(item.open_time_ms) is not int
        or type(item.close_time_ms) is not int
        or item.open_time_ms <= 0
        or item.open_time_ms % period != 0
        or item.close_time_ms != item.open_time_ms + period - 1
        or any(not _price(value) for value in (item.open, item.high, item.low, item.close))
        or not _price(item.volume, allow_zero=True)
    ):
        return False
    return item.low <= min(item.open, item.close) <= max(item.open, item.close) <= item.high


def _valid_probabilities(p: object) -> bool:
    if type(p) is not ProbabilityEstimate:
        return False
    values = (p.win, p.loss, p.flat)
    return (
        all(type(v) is Decimal and v.is_finite() and ZERO <= v <= ONE for v in values)
        and sum(values, ZERO) == ONE
    )


def _candle_payload(item: Candle) -> list[str | int]:
    return [
        item.symbol, item.interval, item.open_time_ms, item.close_time_ms,
        _dec(item.open), _dec(item.high), _dec(item.low),
        _dec(item.close), _dec(item.volume),
    ]


def build_offline_candle_label_ledger(
    candles: Sequence[Candle],
    forecasts: Sequence[RecordedForecast],
    *,
    symbol: str,
    interval: str,
    model_version: str,
    outcome_horizon_ms: int,
    hypothetical_roundtrip_cost_bps: Decimal,
    flat_band_bps: Decimal,
    evaluated_at_ms: int,
) -> OfflineCandleLabelLedger:
    """Recompute LONG-only, fee-stressed close-to-close labels in supplied data.

    Model provenance, candle exchange origin, claimed forecast recording times
    and quoted commission are NOT independently verified. An outcome is
    determined at a CLOSED future candle, not a simulated executable price.
    """
    if (
        symbol not in SUPPORTED_SYMBOLS
        or interval not in INTERVAL_MS
        or type(model_version) is not str
        or not 1 <= len(model_version) <= 64
        or not model_version.isascii()
        or not all(c.isalnum() or c in "-_." for c in model_version)
        or type(outcome_horizon_ms) is not int
        or outcome_horizon_ms <= 0
        or outcome_horizon_ms > MAX_HORIZON_MS
        or outcome_horizon_ms % INTERVAL_MS[interval] != 0
        or type(evaluated_at_ms) is not int
        or evaluated_at_ms <= 0
        or type(candles) not in (list, tuple)
        or type(forecasts) not in (list, tuple)
        or not 2 <= len(candles) <= MAX_CANDLES
        or not 1 <= len(forecasts) <= MAX_FORECASTS
        or not _price(hypothetical_roundtrip_cost_bps, allow_zero=True)
        or hypothetical_roundtrip_cost_bps > MAX_COST_BPS
        or not _price(flat_band_bps, allow_zero=True)
        or flat_band_bps > MAX_FLAT_BPS
    ):
        return OfflineCandleLabelLedger(LabelStatus.INVALID_INPUT)
    period = INTERVAL_MS[interval]
    prev_open: int | None = None
    by_close: dict[int, Candle] = {}
    for item in candles:
        if not _valid_candle(item, symbol, interval, period):
            return OfflineCandleLabelLedger(LabelStatus.INVALID_CANDLES)
        if prev_open is not None and item.open_time_ms != prev_open + period:
            return OfflineCandleLabelLedger(LabelStatus.INVALID_CANDLES)
        prev_open = item.open_time_ms
        by_close[item.close_time_ms] = item
    if candles[-1].close_time_ms >= evaluated_at_ms:
        return OfflineCandleLabelLedger(LabelStatus.FUTURE_INFORMATION_RISK)
    candle_digest = _hash([_candle_payload(c) for c in candles])
    previous = GENESIS
    last_forecast_at = -1
    seen: set[str] = set()
    entries: list[CandleLabelEntry] = []
    for row in forecasts:
        if (
            type(row) is not RecordedForecast
            or type(row.signal_id) is not str
            or not 8 <= len(row.signal_id) <= 128
            or not row.signal_id.isascii()
            or not all(c.isalnum() or c in "_-" for c in row.signal_id)
            or row.signal_id in seen
            or row.model_version != model_version
            or row.symbol != symbol
            or row.interval != interval
            or type(row.observed_close_at_ms) is not int
            or type(row.recorded_at_ms) is not int
            or not _valid_probabilities(row.forecast)
        ):
            return OfflineCandleLabelLedger(LabelStatus.INVALID_FORECAST)
        if row.observed_close_at_ms <= last_forecast_at:
            return OfflineCandleLabelLedger(LabelStatus.FUTURE_INFORMATION_RISK)
        source = by_close.get(row.observed_close_at_ms)
        target = by_close.get(row.observed_close_at_ms + outcome_horizon_ms)
        if source is None or target is None:
            return OfflineCandleLabelLedger(LabelStatus.MISSING_FUTURE_CANDLE)
        if (
            row.recorded_at_ms <= source.close_time_ms
            or row.recorded_at_ms >= source.close_time_ms + period
            or row.recorded_at_ms >= target.close_time_ms
            or row.recorded_at_ms > evaluated_at_ms
        ):
            return OfflineCandleLabelLedger(LabelStatus.FUTURE_INFORMATION_RISK)
        # Subtract explicit, hypothetical roundtrip costs; this is neither
        # a fill nor evidence that a real account paid the stated fee.
        net_bps = (target.close / source.close - ONE) * BPS
        net_bps -= hypothetical_roundtrip_cost_bps
        if net_bps > flat_band_bps:
            outcome = ObservedOutcome.WIN
        elif net_bps < -flat_band_bps:
            outcome = ObservedOutcome.LOSS
        else:
            outcome = ObservedOutcome.FLAT
        labeled = ForecastOutcome(
            signal_id=row.signal_id,
            model_version=model_version,
            symbol=symbol,
            interval=interval,
            observed_close_at_ms=row.observed_close_at_ms,
            label_available_at_ms=target.close_time_ms,
            forecast=row.forecast,
            realized_outcome=outcome,
        )
        digest = _hash(
            {
                "candle_sha256": candle_digest,
                "signal_id": row.signal_id,
                "model_version": model_version,
                "symbol": symbol,
                "interval": interval,
                "source_close_ms": source.close_time_ms,
                "target_close_ms": target.close_time_ms,
                "recorded_at_ms": row.recorded_at_ms,
                "probabilities": [
                    _dec(row.forecast.win), _dec(row.forecast.loss), _dec(row.forecast.flat)
                ],
                "source_price": _dec(source.close),
                "target_price": _dec(target.close),
                "assumed_roundtrip_cost_bps": _dec(hypothetical_roundtrip_cost_bps),
                "flat_band_bps": _dec(flat_band_bps),
                "net_return_bps": _dec(net_bps),
                "outcome": outcome.value,
            },
            previous=previous,
        )
        entries.append(
            CandleLabelEntry(
                previous, digest, labeled, row.recorded_at_ms,
                source.close, target.close, hypothetical_roundtrip_cost_bps, net_bps
            )
        )
        previous = digest
        last_forecast_at = row.observed_close_at_ms
        seen.add(row.signal_id)
    return OfflineCandleLabelLedger(
        LabelStatus.UNANCHORED_RESEARCH_ONLY, symbol, candle_digest, previous, tuple(entries)
    )


def verify_offline_candle_label_ledger(
    ledger: OfflineCandleLabelLedger,
    candles: Sequence[Candle],
    forecasts: Sequence[RecordedForecast],
    *,
    symbol: str,
    interval: str,
    model_version: str,
    outcome_horizon_ms: int,
    hypothetical_roundtrip_cost_bps: Decimal,
    flat_band_bps: Decimal,
    evaluated_at_ms: int,
    detached_head_sha256: str | None,
) -> OfflineCandleLabelLedger:
    """Replay every record against candles and optional external, untrusted hash."""
    if type(ledger) is not OfflineCandleLabelLedger:
        return OfflineCandleLabelLedger(LabelStatus.INVALID_INPUT)
    rebuilt = build_offline_candle_label_ledger(
        candles, forecasts, symbol=symbol, interval=interval, model_version=model_version,
        outcome_horizon_ms=outcome_horizon_ms,
        hypothetical_roundtrip_cost_bps=hypothetical_roundtrip_cost_bps,
        flat_band_bps=flat_band_bps,
        evaluated_at_ms=evaluated_at_ms,
    )
    if rebuilt.status is not LabelStatus.UNANCHORED_RESEARCH_ONLY:
        return rebuilt
    if ledger != rebuilt:
        return OfflineCandleLabelLedger(LabelStatus.ALTERED_RESEARCH_CHAIN)
    if detached_head_sha256 is None:
        return rebuilt
    if (
        type(detached_head_sha256) is not str
        or len(detached_head_sha256) != 64
        or not all(x in "0123456789abcdef" for x in detached_head_sha256)
        or detached_head_sha256 != ledger.head_sha256
    ):
        return OfflineCandleLabelLedger(LabelStatus.DETACHED_REFERENCE_MISMATCH)
    return OfflineCandleLabelLedger(
        LabelStatus.SELF_REPORTED_REPLAY_ONLY,
        ledger.symbol, ledger.candle_sha256, ledger.head_sha256, ledger.entries
    )
