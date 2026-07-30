"""Frozen profitability cohort admission for probability strategies."""

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Optional, Tuple

from .probability_calibration_map import (
    PROBABILITY_MODEL_IDS,
    _frozen_calibration_reason,
)
from .profitability_analysis import PROBABILITY_STRATEGIES, cohort_key


GATE_VERSION = 1
VALIDATION_SECONDS = 72 * 3600
MINIMUM_INDEPENDENT_MARKETS = 50
MAXIMUM_POSITIVE_MARKET_SHARE = 0.25
_THRESHOLDS = {
    "minimum_independent_markets": MINIMUM_INDEPENDENT_MARKETS,
    "minimum_mean_net_return_exclusive": 0.0,
    "minimum_lower_bound_95_exclusive": 0.0,
    "maximum_positive_market_share": MAXIMUM_POSITIVE_MARKET_SHARE,
}
_COHORT_DIMENSIONS = (
    "strategy",
    "asset",
    "timeframe",
    "outcome",
    "probability",
    "fill",
    "seconds",
)


def canonical_payload_hash(
    payload: dict,
    excluded_fields: tuple[str, ...] = ("content_hash",),
) -> str:
    """Hash a JSON payload while excluding only named top-level fields."""
    filtered = {
        key: value
        for key, value in payload.items()
        if key not in excluded_fields
    }
    encoded = json.dumps(
        filtered,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _atomic_publish(payload, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _validated_report(report):
    if not isinstance(report, dict) or report.get("version") != 1:
        raise ValueError("profitability report schema is invalid")
    blocking = report.get("blocking_exclusions")
    if not isinstance(blocking, dict):
        raise ValueError("profitability report blocking_exclusions is invalid")
    if blocking:
        reasons = ",".join(sorted(str(reason) for reason in blocking))
        raise ValueError(f"profitability report blocking_exclusions: {reasons}")
    cohorts = report.get("cohorts")
    source_hashes = report.get("selected_config_hashes")
    if not isinstance(cohorts, dict) or not isinstance(source_hashes, dict):
        raise ValueError("profitability report cohorts or config hashes are invalid")
    if any(
        strategy not in PROBABILITY_STRATEGIES
        or not isinstance(value, str)
        or not value
        for strategy, value in source_hashes.items()
    ):
        raise ValueError("profitability report config hashes are invalid")
    for field in ("real_order_submissions", "real_orders", "real_fills"):
        if type(report.get(field)) is not int or report[field] != 0:
            raise ValueError(f"profitability report {field} must equal zero")
    return cohorts, source_hashes


def _cohort_rejection(cohort):
    if not isinstance(cohort, dict):
        return "invalid_cohort"
    markets = cohort.get("independent_markets")
    if type(markets) is not int or markets < MINIMUM_INDEPENDENT_MARKETS:
        return "insufficient_independent_markets"
    mean = _finite(cohort.get("mean_net_return"))
    if mean is None or mean <= 0:
        return "mean_net_return_not_positive"
    lower = _finite(cohort.get("lower_bound_95"))
    if lower is None or lower <= 0:
        return "lower_bound_95_not_positive"
    share = _finite(cohort.get("largest_positive_market_share"))
    if share is None or not 0 <= share <= MAXIMUM_POSITIVE_MARKET_SHARE:
        return "positive_pnl_too_concentrated"
    return None


def _dimensions_key(dimensions):
    if not isinstance(dimensions, dict) or any(
        not isinstance(dimensions.get(name), str) or not dimensions[name]
        for name in _COHORT_DIMENSIONS
    ):
        return None
    return "|".join(f"{name}={dimensions[name]}" for name in _COHORT_DIMENSIONS)


def build_profitability_gate(
    report: dict,
    calibration_snapshot: dict,
    target_base_config_hashes: dict[str, str],
    now: float,
    cohort_version: str,
) -> dict:
    """Build a content-bound gate, retaining exact rejection diagnostics."""
    cohorts, source_hashes = _validated_report(report)
    snapshot_reason = _frozen_calibration_reason(calibration_snapshot, now)
    if snapshot_reason:
        raise ValueError(snapshot_reason)
    if not isinstance(target_base_config_hashes, dict):
        raise ValueError("target_base_config_hashes is invalid")
    normalized_targets = {
        strategy: value
        for strategy, value in sorted(target_base_config_hashes.items())
        if strategy in PROBABILITY_STRATEGIES
        and isinstance(value, str)
        and value
    }
    if not isinstance(cohort_version, str) or not cohort_version.strip():
        raise ValueError("profitability cohort version is invalid")
    activated = _finite(now)
    if activated is None:
        raise ValueError("profitability gate activation is invalid")
    if any(
        normalized_targets.get(strategy) != value
        for strategy, value in source_hashes.items()
    ):
        raise ValueError("source discovery config hashes do not match targets")

    eligible = {}
    rejected = {}
    for key, cohort in sorted(cohorts.items()):
        reason = _cohort_rejection(cohort)
        dimensions = cohort.get("dimensions", {}) if isinstance(cohort, dict) else {}
        strategy = dimensions.get("strategy") if isinstance(dimensions, dict) else None
        if reason is None and _dimensions_key(dimensions) != str(key):
            reason = "cohort_key_mismatch"
        if reason is None and strategy not in PROBABILITY_STRATEGIES:
            reason = "unknown_strategy"
        if reason is None and strategy not in normalized_targets:
            reason = "target_base_config_hash_unavailable"
        calibration_entry = calibration_snapshot["strategies"].get(strategy, {})
        calibration_cohort = (
            calibration_entry.get("cohort", {})
            if isinstance(calibration_entry, dict)
            else {}
        )
        if reason is None and (
            calibration_cohort.get("strategy_config_hash")
            != normalized_targets[strategy]
            or calibration_cohort.get("probability_model_id")
            != PROBABILITY_MODEL_IDS[strategy]
        ):
            reason = "calibration_cohort_mismatch"
        entry = dict(cohort) if isinstance(cohort, dict) else {"source": cohort}
        if reason is None:
            entry.update({
                "decision": "ALLOW",
                "reason": "profitability_cohort_eligible",
                "source_discovery_config_hash": source_hashes[strategy],
                "strategy_base_config_hash": normalized_targets[strategy],
                "probability_model_id": PROBABILITY_MODEL_IDS[strategy],
            })
            eligible[str(key)] = entry
        else:
            entry.update({"decision": "BLOCK", "reason": reason})
            rejected[str(key)] = entry

    payload = {
        "version": GATE_VERSION,
        "generated_at": activated,
        "validation_activated_at": activated,
        "validation_expires_at": min(
            activated + VALIDATION_SECONDS,
            float(calibration_snapshot["validation_expires_at"]),
        ),
        "profitability_cohort_version": cohort_version,
        "calibration_snapshot_hash": calibration_snapshot["content_hash"],
        "source": report.get("source", {}),
        "source_discovery_config_hashes": {
            strategy: value
            for strategy, value in sorted(source_hashes.items())
            if strategy in PROBABILITY_STRATEGIES
        },
        "target_base_config_hashes": normalized_targets,
        "probability_model_ids": {
            strategy: PROBABILITY_MODEL_IDS[strategy]
            for strategy in normalized_targets
        },
        "thresholds": dict(_THRESHOLDS),
        "decision": "ALLOW" if eligible else "NO_TRADE",
        "eligible_cohorts": eligible,
        "rejected_cohorts": rejected,
    }
    payload["content_hash"] = canonical_payload_hash(payload)
    return payload


def _gate_reason(payload, now):
    current = _finite(now)
    if current is None:
        return "profitability_gate_invalid"
    if not isinstance(payload, dict) or payload.get("version") != GATE_VERSION:
        return "profitability_gate_invalid"
    for field in (
        "eligible_cohorts",
        "rejected_cohorts",
        "target_base_config_hashes",
        "source_discovery_config_hashes",
        "probability_model_ids",
        "thresholds",
    ):
        if not isinstance(payload.get(field), dict):
            return "profitability_gate_invalid"
    content_hash = payload.get("content_hash")
    if (
        not isinstance(content_hash, str)
        or content_hash != canonical_payload_hash(payload)
    ):
        return "profitability_gate_hash_mismatch"
    thresholds = payload["thresholds"]
    if (
        set(thresholds) != set(_THRESHOLDS)
        or any(isinstance(value, bool) for value in thresholds.values())
        or thresholds != _THRESHOLDS
    ):
        return "profitability_gate_invalid"
    source_hashes = payload["source_discovery_config_hashes"]
    target_hashes = payload["target_base_config_hashes"]
    model_ids = payload["probability_model_ids"]
    if (
        any(
            strategy not in PROBABILITY_STRATEGIES
            or not isinstance(value, str)
            or not value
            or target_hashes.get(strategy) != value
            for strategy, value in source_hashes.items()
        )
        or any(
            strategy not in PROBABILITY_STRATEGIES
            or not isinstance(value, str)
            or not value
            for strategy, value in target_hashes.items()
        )
        or set(model_ids) != set(target_hashes)
        or any(
            model_ids.get(strategy) != PROBABILITY_MODEL_IDS[strategy]
            for strategy in target_hashes
        )
    ):
        return "profitability_gate_invalid"
    cohort_version = payload.get("profitability_cohort_version")
    snapshot_hash = payload.get("calibration_snapshot_hash")
    if (
        not isinstance(cohort_version, str)
        or not cohort_version
        or not isinstance(snapshot_hash, str)
        or len(snapshot_hash) != 64
        or any(character not in "0123456789abcdef" for character in snapshot_hash)
        or payload.get("decision") not in {"ALLOW", "NO_TRADE"}
        or (payload["decision"] == "ALLOW") != bool(payload["eligible_cohorts"])
    ):
        return "profitability_gate_invalid"
    for key, entry in payload["eligible_cohorts"].items():
        if (
            not isinstance(key, str)
            or not isinstance(entry, dict)
            or _dimensions_key(entry.get("dimensions")) != key
            or entry.get("decision") != "ALLOW"
            or entry.get("reason") != "profitability_cohort_eligible"
            or _cohort_rejection(entry) is not None
        ):
            return "profitability_gate_invalid"
        strategy = entry["dimensions"]["strategy"]
        discovery_hash = source_hashes.get(strategy)
        if (
            not discovery_hash
            or entry.get("source_discovery_config_hash") != discovery_hash
            or entry.get("strategy_base_config_hash") != discovery_hash
            or target_hashes.get(strategy) != discovery_hash
            or entry.get("probability_model_id")
            != model_ids.get(strategy)
        ):
            return "profitability_gate_invalid"
    activated = _finite(payload.get("validation_activated_at"))
    expires = _finite(payload.get("validation_expires_at"))
    if (
        activated is None
        or expires is None
        or expires > activated + VALIDATION_SECONDS
        or current < activated
    ):
        return "profitability_gate_invalid"
    if current >= expires:
        return "profitability_gate_expired"
    return None


def publish_profitability_gate(payload: dict, path: Path) -> None:
    """Atomically publish a previously constructed, self-hashed gate."""
    activated = (
        _finite(payload.get("validation_activated_at"))
        if isinstance(payload, dict)
        else None
    )
    if activated is None or _gate_reason(payload, activated):
        raise ValueError("refusing to publish invalid profitability gate")
    _atomic_publish(payload, path)


def load_profitability_gate(
    path: Path,
    now: float,
) -> Tuple[Optional[dict], Optional[str]]:
    """Load and validate a gate without accepting malformed content."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "profitability_gate_unavailable"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "profitability_gate_invalid"
    reason = _gate_reason(payload, now)
    return (None, reason) if reason else (payload, None)


def evaluate_profitability_gate(
    row: dict,
    payload: Optional[dict],
    now: float,
    expected: dict,
) -> dict:
    """Match a live probability-strategy row against a frozen gate."""
    gate_hash = payload.get("content_hash") if isinstance(payload, dict) else None
    snapshot_hash = (
        payload.get("calibration_snapshot_hash")
        if isinstance(payload, dict)
        else None
    )
    try:
        key = cohort_key(row)
    except (KeyError, TypeError, ValueError):
        key = None

    def result(decision, reason):
        return {
            "decision": decision,
            "reason": reason,
            "cohort_key": key,
            "gate_content_hash": gate_hash,
            "calibration_snapshot_hash": snapshot_hash,
        }

    if (
        not isinstance(payload, dict)
        or not isinstance(expected, dict)
        or _gate_reason(payload, now)
        or key is None
    ):
        return result("BLOCK", "profitability_gate_unavailable")
    strategy = row.get("strategy")
    checks = (
        payload["target_base_config_hashes"].get(strategy)
        == expected.get("strategy_base_config_hash"),
        payload["probability_model_ids"].get(strategy)
        == expected.get("probability_model_id"),
        payload.get("calibration_snapshot_hash")
        == expected.get("calibration_snapshot_hash"),
        payload.get("profitability_cohort_version")
        == expected.get("profitability_cohort_version"),
    )
    if not all(checks):
        return result("BLOCK", "profitability_gate_unavailable")
    entry = payload["eligible_cohorts"].get(key)
    if not isinstance(entry, dict) or entry.get("decision") != "ALLOW":
        return result("BLOCK", "profitability_cohort_not_eligible")
    if _cohort_rejection(entry) is not None:
        return result("BLOCK", "profitability_cohort_not_eligible")
    return result("ALLOW", "profitability_cohort_eligible")
