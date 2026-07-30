"""Publish the probability calibration map consumed by the C++ engine.

The C++ market_ws_engine is the canonical ACCEPT producer for the probability
strategies, but the calibration evidence (shadow_prediction_complete rows) is
produced by the Python shadow lifecycle. This module aggregates that evidence
into data/probability-calibration-map.json so the engine can replace raw model
probabilities with empirically calibrated ones in its EV gate.

The shrinkage formula here must stay byte-identical to the C++ consumer
(cpp/market_ws_engine/market_ws_engine.cpp) and the Python parity verifier:

    calibrated = (actual_up + prior_weight * expected_up_rate) / (samples + prior_weight)

Buckets with fewer than min_bucket_samples fall back to the strategy-level
(all-timeframe) bucket; if that is also insufficient the consumer fails closed
(probability_calibration_unavailable).
"""
import json
import math
import os
import time
from pathlib import Path

from .jsonl_history import history_paths, open_history

STRATEGIES = ("late_window_directional_ev", "low_price_lottery_ev")
PROBABILITY_MODEL_IDS = {
    "late_window_directional_ev": "directional_logistic_projected_v2",
    "low_price_lottery_ev": "lottery_logistic_projected_blend_v2",
}
DEFAULT_MIN_BUCKET_SAMPLES = 30
DEFAULT_PRIOR_WEIGHT = 30.0
DEFAULT_PUBLISH_SECONDS = 30.0
MAP_VERSION = 2
VALIDATION_SECONDS = 72 * 3600
_BUCKET_NAMES = frozenset(
    f"{index / 10:.1f}-{(index + 1) / 10:.1f}"
    for index in range(10)
)


def bucket_index(probability):
    return min(9, max(0, int(float(probability) * 10)))


def bucket_name(index):
    return f"{index / 10:.1f}-{(index + 1) / 10:.1f}"


def min_bucket_samples_from_env():
    return int(os.getenv(
        "PROBABILITY_CALIBRATION_MIN_BUCKET_SAMPLES", str(DEFAULT_MIN_BUCKET_SAMPLES)))


def prior_weight_from_env():
    return float(os.getenv(
        "PROBABILITY_CALIBRATION_PRIOR_WEIGHT", str(DEFAULT_PRIOR_WEIGHT)))


def publish_seconds_from_env():
    return float(os.getenv(
        "PROBABILITY_CALIBRATION_MAP_PUBLISH_SECONDS", str(DEFAULT_PUBLISH_SECONDS)))


def _prediction_rows(path):
    for history_path in history_paths(path):
        if not history_path.exists():
            continue
        with open_history(history_path) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (row.get("event_type") == "shadow_prediction_complete"
                        and row.get("strategy") in STRATEGIES
                        and row.get("estimated_up_probability") is not None
                        and row.get("actual_up") is not None
                        and row.get("timeframe")):
                    yield row


def _bucket_view(entry):
    samples = entry["samples"]
    if not samples:
        return None
    return {
        "samples": samples,
        "expected_up_rate": round(entry["sum_probability"] / samples, 12),
        "realized_up_rate": round(entry["actual_up"] / samples, 12),
    }


def build_calibration_map(execution_path, min_bucket_samples=None,
                          prior_weight=None, now=None, strategy_cohorts=None):
    """Aggregate shadow_prediction_complete history into the map payload.

    Layout: strategies -> {timeframes -> {tf -> {bucket: stats}},
                           overall -> {bucket: stats}}.
    """
    min_bucket_samples = (min_bucket_samples_from_env()
                          if min_bucket_samples is None else int(min_bucket_samples))
    prior_weight = (prior_weight_from_env()
                    if prior_weight is None else float(prior_weight))
    aggregate = {
        strategy: {"timeframes": {}, "overall": {}}
        for strategy in STRATEGIES
    }
    excluded_other_cohort = {strategy: 0 for strategy in STRATEGIES}

    def _entry(scope, bucket):
        return scope.setdefault(bucket, {
            "samples": 0, "sum_probability": 0.0, "actual_up": 0,
        })

    for row in _prediction_rows(execution_path):
        strategy = row["strategy"]
        expected_cohort = (strategy_cohorts or {}).get(strategy)
        if expected_cohort and any(
            row.get(field) != expected_cohort.get(field)
            for field in ("strategy_config_hash", "probability_model_id")
        ):
            excluded_other_cohort[strategy] += 1
            continue
        timeframe = row["timeframe"]
        probability = float(row["estimated_up_probability"])
        if not 0 <= probability <= 1:
            continue
        actual = int(row["actual_up"])
        name = bucket_name(bucket_index(probability))
        for scope in (aggregate[strategy]["timeframes"].setdefault(timeframe, {}),
                      aggregate[strategy]["overall"]):
            entry = _entry(scope, name)
            entry["samples"] += 1
            entry["sum_probability"] += probability
            entry["actual_up"] += actual

    strategies = {}
    for strategy, scopes in aggregate.items():
        strategies[strategy] = {
            "cohort": dict((strategy_cohorts or {}).get(strategy, {})),
            "timeframes": {
                timeframe: {
                    name: view for name, view in (
                        (name, _bucket_view(entry)) for name, entry in sorted(buckets.items())
                    ) if view
                }
                for timeframe, buckets in sorted(scopes["timeframes"].items())
            },
            "overall": {
                name: view for name, view in (
                    (name, _bucket_view(entry)) for name, entry in sorted(scopes["overall"].items())
                ) if view
            },
        }
    return {
        "version": MAP_VERSION,
        "generated_at": float(now if now is not None else time.time()),
        "config": {
            "min_bucket_samples": min_bucket_samples,
            "prior_weight": prior_weight,
        },
        "excluded_other_cohort": excluded_other_cohort,
        "strategies": strategies,
    }


def publish_calibration_map(execution_path, output_path, min_bucket_samples=None,
                            prior_weight=None, now=None, strategy_cohorts=None):
    """Build the map and atomically publish it (tmp file + os.replace)."""
    payload = build_calibration_map(
        execution_path, min_bucket_samples, prior_weight, now, strategy_cohorts)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, output_path)
    return payload


def _finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _bucket_map_error(buckets, minimum_samples):
    if not isinstance(buckets, dict) or not buckets:
        return "calibration map bucket schema is invalid"
    usable = False
    for name, stats in buckets.items():
        if (
            name not in _BUCKET_NAMES
            or not isinstance(stats, dict)
            or set(stats) != {
                "samples",
                "expected_up_rate",
                "realized_up_rate",
            }
        ):
            return "calibration map bucket schema is invalid"
        samples = stats.get("samples")
        expected = _finite_number(stats.get("expected_up_rate"))
        realized = _finite_number(stats.get("realized_up_rate"))
        if (
            type(samples) is not int
            or samples <= 0
            or expected is None
            or realized is None
            or not 0 <= expected <= 1
            or not 0 <= realized <= 1
        ):
            return "calibration map bucket values are invalid"
        usable = usable or samples >= minimum_samples
    return None if usable else "calibration map bucket samples are insufficient"


def _calibration_payload_error(payload, reference_time=None):
    if not isinstance(payload, dict) or payload.get("version") != MAP_VERSION:
        return "calibration map version or schema is invalid"
    generated_at = _finite_number(payload.get("generated_at"))
    if (
        generated_at is None
        or generated_at <= 0
        or (
            reference_time is not None
            and generated_at > float(reference_time)
        )
    ):
        return "calibration map generated_at is invalid"
    config = payload.get("config")
    if not isinstance(config, dict) or set(config) != {
        "min_bucket_samples",
        "prior_weight",
    }:
        return "calibration map config is invalid"
    minimum_samples = config.get("min_bucket_samples")
    prior_weight = _finite_number(config.get("prior_weight"))
    if (
        type(minimum_samples) is not int
        or minimum_samples <= 0
        or prior_weight is None
        or prior_weight < 0
    ):
        return "calibration map config is invalid"
    excluded = payload.get("excluded_other_cohort")
    if (
        not isinstance(excluded, dict)
        or set(excluded) != set(STRATEGIES)
        or any(
            type(excluded[strategy]) is not int
            or excluded[strategy] < 0
            for strategy in STRATEGIES
        )
    ):
        return "calibration map exclusion counts are invalid"
    strategies = payload.get("strategies")
    if not isinstance(strategies, dict) or set(strategies) != set(STRATEGIES):
        return "calibration map strategy schema is invalid"
    for strategy in STRATEGIES:
        entry = strategies[strategy]
        if not isinstance(entry, dict) or set(entry) != {
            "cohort",
            "timeframes",
            "overall",
        }:
            return "calibration map strategy schema is invalid"
        cohort = entry["cohort"]
        if (
            not isinstance(cohort, dict)
            or set(cohort) != {
                "strategy_config_hash",
                "probability_model_id",
            }
            or not isinstance(cohort.get("strategy_config_hash"), str)
            or not cohort["strategy_config_hash"]
            or cohort.get("probability_model_id")
            != PROBABILITY_MODEL_IDS[strategy]
        ):
            return "calibration map cohort identity is invalid"
        timeframes = entry["timeframes"]
        if (
            not isinstance(timeframes, dict)
            or not timeframes
            or any(
                not isinstance(timeframe, str)
                or not timeframe
                for timeframe in timeframes
            )
        ):
            return "calibration map strategy schema is invalid"
        for buckets in timeframes.values():
            error = _bucket_map_error(buckets, minimum_samples)
            if error and error != "calibration map bucket samples are insufficient":
                return error
        overall_error = _bucket_map_error(entry["overall"], minimum_samples)
        if overall_error:
            return overall_error
    return None


def _validated_rolling_payload(source):
    try:
        payload = json.loads(Path(source).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("calibration map is missing") from exc
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("calibration map is corrupt") from exc
    if any(
        field in payload
        for field in (
            "content_hash",
            "validation_activated_at",
            "validation_expires_at",
        )
    ):
        raise ValueError("rolling calibration map contains frozen fields")
    error = _calibration_payload_error(payload)
    if error:
        raise ValueError(error)
    return payload


def _build_frozen_calibration_snapshot(source, now):
    from .profitability_gate import canonical_payload_hash

    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(float(now))
    ):
        raise ValueError("calibration snapshot activation is invalid")
    payload = _validated_rolling_payload(source)
    generated_at = float(payload["generated_at"])
    if generated_at > float(now):
        raise ValueError("calibration map generated_at is invalid")
    snapshot = json.loads(json.dumps(payload))
    snapshot["validation_activated_at"] = float(now)
    snapshot["validation_expires_at"] = float(now) + VALIDATION_SECONDS
    snapshot["content_hash"] = canonical_payload_hash(snapshot)
    return snapshot


def _publish_frozen_calibration_snapshot(payload, destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, destination)


def freeze_calibration_snapshot(source: Path, destination: Path, now: float) -> dict:
    """Copy a rolling version-2 map into an immutable 72-hour snapshot."""
    if Path(source).resolve() == Path(destination).resolve():
        raise ValueError("calibration source and destination must be distinct")
    snapshot = _build_frozen_calibration_snapshot(source, now)
    _publish_frozen_calibration_snapshot(snapshot, destination)
    return snapshot


def _frozen_calibration_reason(payload, now, expected_content_hash=None):
    from .profitability_gate import canonical_payload_hash

    if (
        isinstance(now, bool)
        or not isinstance(now, (int, float))
        or not math.isfinite(float(now))
    ):
        return "calibration_snapshot_invalid"
    if not isinstance(payload, dict) or payload.get("version") != MAP_VERSION:
        return "calibration_snapshot_invalid"
    activated = payload.get("validation_activated_at")
    expires = payload.get("validation_expires_at")
    if (
        isinstance(activated, bool)
        or isinstance(expires, bool)
        or not isinstance(activated, (int, float))
        or not isinstance(expires, (int, float))
        or not math.isfinite(float(activated))
        or not math.isfinite(float(expires))
        or float(expires) != float(activated) + VALIDATION_SECONDS
        or float(now) < float(activated)
    ):
        return "calibration_snapshot_invalid"
    content_hash = payload.get("content_hash")
    if (
        not isinstance(content_hash, str)
        or content_hash != canonical_payload_hash(payload)
        or (
            expected_content_hash is not None
            and content_hash != expected_content_hash
        )
    ):
        return "calibration_snapshot_hash_mismatch"
    if _calibration_payload_error(payload, activated):
        return "calibration_snapshot_invalid"
    if float(now) >= float(expires):
        return "calibration_snapshot_expired"
    return None


def load_frozen_calibration_snapshot(
    path: Path,
    now: float,
    expected_content_hash=None,
):
    """Load a frozen snapshot, returning ``(payload, reason)`` fail closed."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "calibration_snapshot_unavailable"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "calibration_snapshot_invalid"
    reason = _frozen_calibration_reason(
        payload,
        now,
        expected_content_hash,
    )
    if reason:
        return None, reason
    return payload, None


# --- Consumer side (Python parity verifier; mirrors the C++ engine byte-for-byte) ---

DEFAULT_MAP_PATH = "data/probability-calibration-map.json"
DEFAULT_MAP_MAX_AGE_SECONDS = 120.0
_MAP_CACHE = {}


def map_max_age_seconds_from_env():
    return float(os.getenv(
        "PROBABILITY_CALIBRATION_MAP_MAX_AGE_SECONDS", str(int(DEFAULT_MAP_MAX_AGE_SECONDS))))


def require_map_from_env():
    return os.getenv("PROBABILITY_CALIBRATION_REQUIRE_MAP", "1").strip().lower() not in {
        "0", "false", "no", "off",
    }


def load_calibration_map(path=None):
    """Load the published map with an mtime cache; None when unreadable."""
    path = str(path or os.getenv("PROBABILITY_CALIBRATION_MAP_PATH", DEFAULT_MAP_PATH))
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return None
    cached = _MAP_CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    _MAP_CACHE[path] = (mtime, payload)
    return payload


def calibrate_probability(payload, strategy, timeframe, up_probability, now,
                          min_bucket_samples=None, prior_weight=None,
                          require_map=None, max_age_seconds=None,
                          expected_config_hash=None, expected_model_id=None):
    """Mirror of the C++ CalibrationResult / calibrate_probability.

    Returns dict(input_probability, probability, bucket, samples, scope) where
    probability is the calibrated Up probability (None when the map cannot
    vouch for it and require_map is on). scope: timeframe | strategy | raw | none.
    """
    min_bucket_samples = (min_bucket_samples_from_env()
                          if min_bucket_samples is None else int(min_bucket_samples))
    prior_weight = (prior_weight_from_env()
                    if prior_weight is None else float(prior_weight))
    require_map = require_map_from_env() if require_map is None else bool(require_map)
    max_age_seconds = (map_max_age_seconds_from_env()
                       if max_age_seconds is None else float(max_age_seconds))
    result = {
        "input_probability": up_probability, "probability": None,
        "bucket": None, "samples": 0, "scope": "none",
    }
    if up_probability is None:
        return result
    result["bucket"] = bucket_name(bucket_index(up_probability))

    def raw_fallback():
        if not require_map:
            result["probability"] = up_probability
            result["scope"] = "raw"

    if int((payload or {}).get("version", 0) or 0) != MAP_VERSION:
        raw_fallback()
        return result
    generated_at = float((payload or {}).get("generated_at", 0) or 0)
    map_age = now - generated_at if generated_at > 0 else float("inf")
    if not map_age <= max_age_seconds:
        raw_fallback()
        return result
    strategies = (payload or {}).get("strategies", {})
    entry = strategies.get(strategy)
    if entry is None:
        raw_fallback()
        return result
    cohort = entry.get("cohort", {})
    if (
        expected_config_hash is not None
        and cohort.get("strategy_config_hash") != expected_config_hash
    ) or (
        expected_model_id is not None
        and cohort.get("probability_model_id") != expected_model_id
    ):
        raw_fallback()
        return result
    selected = None
    scope = None
    bucket = entry.get("timeframes", {}).get(timeframe, {}).get(result["bucket"])
    if bucket and int(bucket.get("samples", 0)) >= min_bucket_samples:
        selected, scope = bucket, "timeframe"
    if selected is None:
        bucket = entry.get("overall", {}).get(result["bucket"])
        if bucket and int(bucket.get("samples", 0)) >= min_bucket_samples:
            selected, scope = bucket, "strategy"
    if selected is None:
        raw_fallback()
        return result
    samples = int(selected["samples"])
    calibrated = (
        float(selected["realized_up_rate"]) * samples
        + prior_weight * float(selected["expected_up_rate"])
    ) / (samples + prior_weight)
    result["samples"] = samples
    result["scope"] = scope
    result["probability"] = min(0.999, max(0.001, calibrated))
    return result
