"""Machine-executable forward Shadow profitability acceptance."""

import hashlib
import json
import math
import os
import statistics
import time
from collections import Counter
from pathlib import Path

from .ev_shadow import canonical_strategy_base_config_hash
from .jsonl_history import history_paths, open_history
from .probability_calibration_map import load_frozen_calibration_snapshot
from .profitability_analysis import (
    PROBABILITY_STRATEGIES,
    block_bootstrap_lower_bound,
    cohort_key,
)
from .profitability_gate import (
    canonical_payload_hash,
    load_profitability_gate,
)


ACCEPTANCE_VERSION = 1
MINIMUM_RUNTIME_SECONDS = 48 * 3600
MINIMUM_INDEPENDENT_MARKETS = 300
MINIMUM_COHORT_MARKETS = 50
MAXIMUM_DRAWDOWN_FRACTION = 0.10
_ACCEPTANCE_FIELDS = {
    "version",
    "generated_at",
    "status",
    "classification",
    "reason",
    "exit_code",
    "validation_started_at",
    "validation_expires_at",
    "enabled_cohorts",
    "sample_counts",
    "metrics",
    "identities",
    "source",
    "excluded",
    "checks",
    "real_order_submissions",
    "real_orders",
    "real_fills",
    "content_hash",
}
_REAL_ORDER_FIELDS = (
    "real_order_submissions",
    "real_orders",
    "real_fills",
)
_HASH_FIELDS = (
    "profitability_gate_hash",
    "calibration_snapshot_hash",
)


def _finite(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _close(left, right, tolerance=1e-8):
    return math.isclose(left, right, rel_tol=0, abs_tol=tolerance)


def _valid_hash(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def expected_strategy_config_hash(gate: dict, strategy: str) -> str:
    """Return the final producer hash bound to this exact frozen gate."""
    binding = {
        "probability_validation_calibration_content_hash":
            gate["calibration_snapshot_hash"],
        "profitability_cohort_version":
            gate["profitability_cohort_version"],
        "profitability_gate_content_hash": gate["content_hash"],
        "shadow_cash_ledger_version": "2",
        "strategy_base_config_hash": gate["target_base_config_hashes"][
            strategy
        ],
    }
    encoded = json.dumps(
        binding, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _empty_metrics(starting_capital=None):
    return {
        "runtime_seconds": None,
        "independent_markets": 0,
        "completed": 0,
        "sample_count": 0,
        "total_pnl": 0.0,
        "total_pnl_usd": 0.0,
        "mean_return": None,
        "mean_net_return": None,
        "mean_net_return_per_dollar_risked": None,
        "maximum_drawdown_usd": None,
        "maximum_drawdown_pct": None,
        "lower_bound_95": None,
        "confidence_lower_bound_95": None,
        "starting_shadow_capital_usd": starting_capital,
    }


def _result(
    *,
    now,
    status,
    classification,
    reason,
    exit_code,
    gate=None,
    metrics=None,
    sample_counts=None,
    excluded=None,
    source=None,
    real_orders=None,
):
    gate = gate if isinstance(gate, dict) else {}
    counters = real_orders if isinstance(real_orders, dict) else {
        field: None for field in _REAL_ORDER_FIELDS
    }
    payload = {
        "version": ACCEPTANCE_VERSION,
        "generated_at": now,
        "status": status,
        "classification": classification,
        "reason": reason,
        "exit_code": exit_code,
        "validation_started_at": gate.get("validation_activated_at"),
        "validation_expires_at": gate.get("validation_expires_at"),
        "enabled_cohorts": {
            key: entry.get("dimensions")
            for key, entry in gate.get("eligible_cohorts", {}).items()
        },
        "sample_counts": sample_counts or {},
        "metrics": metrics or _empty_metrics(),
        "identities": {
            "profitability_gate_hash": gate.get("content_hash"),
            "calibration_snapshot_hash": gate.get(
                "calibration_snapshot_hash"
            ),
            "profitability_cohort_version": gate.get(
                "profitability_cohort_version"
            ),
            "target_base_config_hashes": gate.get(
                "target_base_config_hashes", {}
            ),
            "probability_model_ids": gate.get("probability_model_ids", {}),
        },
        "source": source or {"path": None, "files": []},
        "excluded": dict(sorted((excluded or {}).items())),
        "checks": [],
        **counters,
    }
    payload["content_hash"] = canonical_payload_hash(payload)
    return payload


def _configuration_error(now, reason, gate=None, **kwargs):
    return _result(
        now=now,
        status="INCOMPLETE",
        classification="CONFIGURATION_ERROR",
        reason=reason,
        exit_code=3,
        gate=gate,
        **kwargs,
    )


def _read_state(path):
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "strategy_state_unavailable"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "strategy_state_invalid"
    if not isinstance(state, dict):
        return None, "strategy_state_invalid"
    return state, None


def _source_rows(path):
    rows = []
    files = []
    invalid_json = 0
    try:
        for candidate in history_paths(Path(path)):
            candidate = Path(candidate)
            if not candidate.exists():
                continue
            encoded = candidate.read_bytes()
            files.append({
                "path": str(candidate),
                "bytes": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
            })
            with open_history(candidate) as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except (UnicodeError, json.JSONDecodeError):
                        invalid_json += 1
                        continue
                    if not isinstance(row, dict):
                        invalid_json += 1
                        continue
                    rows.append(row)
    except (OSError, EOFError, UnicodeError):
        return None, None, "execution_log_read_error"
    if not files:
        return None, None, "execution_log_unavailable"
    return rows, {
        "path": str(Path(path)),
        "files": files,
    }, "ledger_corrupt" if invalid_json else None


def _real_order_reason(rows):
    for row in rows:
        for field in _REAL_ORDER_FIELDS:
            value = row.get(field)
            if type(value) is not int:
                return "ledger_corrupt"
            if value != 0:
                return "real_order_invariant"
    return None


def _identity_reason(row, gate):
    strategy = row.get("strategy")
    if strategy not in PROBABILITY_STRATEGIES:
        return "strategy_identity_mismatch"
    if row.get("profitability_gate_hash") != gate["content_hash"]:
        return "profitability_gate_hash_mismatch"
    if (
        row.get("calibration_snapshot_hash")
        != gate["calibration_snapshot_hash"]
    ):
        return "calibration_snapshot_hash_mismatch"
    if (
        row.get("strategy_config_hash")
        != expected_strategy_config_hash(gate, strategy)
    ):
        return "strategy_config_hash_mismatch"
    if (
        row.get("probability_model_id")
        != gate["probability_model_ids"].get(strategy)
    ):
        return "probability_model_id_mismatch"
    stored_key = row.get("profitability_cohort_key")
    try:
        actual_key = cohort_key(row)
    except (KeyError, TypeError, ValueError):
        return "profitability_cohort_identity_mismatch"
    if stored_key != actual_key or stored_key not in gate["eligible_cohorts"]:
        return "profitability_cohort_identity_mismatch"
    if (
        row.get("profitability_gate_decision") != "ALLOW"
        or row.get("profitability_gate_reason")
        != "profitability_cohort_eligible"
        or row.get("deployable_candidate") is not True
        or row.get("risk_mode") != "PORTFOLIO_LIMITS_ENFORCED"
        or row.get("portfolio_limits_enforced") is not True
        or row.get("deployable_pnl") is not True
        or row.get("cash_ledger_version") != 2
    ):
        return "deployable_classification_mismatch"
    return None


def _ledger_trade(row, now):
    entry_ts = _finite(row.get("entry_ts"))
    close_ts = _finite(row.get("close_ts"))
    completion_ts = _finite(row.get("ts", row.get("timestamp")))
    target_size = _finite(row.get("target_size"))
    entry_cost = _finite(row.get("entry_cost"))
    vwap = _finite(row.get("dynamic_vwap"))
    fee = _finite(row.get("dynamic_fee"))
    if (
        not isinstance(row.get("event_id"), str)
        or not row["event_id"]
        or not isinstance(row.get("market_id"), str)
        or not row["market_id"]
        or entry_ts is None
        or close_ts is None
        or completion_ts is None
        or completion_ts > now
        or not entry_ts < close_ts
        or completion_ts <= entry_ts
        or target_size is None
        or target_size <= 0
        or entry_cost is None
        or entry_cost <= 0
        or vwap is None
        or not 0 < vwap < 1
        or fee is None
        or fee < 0
        or not _close(entry_cost, target_size * vwap + fee)
    ):
        return None

    if (
        row.get("completion_reason") == "profit_target_book_executable"
        or row.get("exit_vwap") is not None
    ):
        quantity = _finite(row.get("exit_fill_quantity"))
        exit_vwap = _finite(row.get("exit_vwap"))
        exit_fee = _finite(row.get("exit_total_fee"))
        proceeds = _finite(row.get("exit_cash_proceeds"))
        if (
            quantity is None
            or not _close(quantity, target_size)
            or exit_vwap is None
            or not 0 < exit_vwap < 1
            or exit_fee is None
            or exit_fee < 0
            or proceeds is None
            or row.get("exit_depth_ok") is not True
            or row.get("exit_book_fresh") is not True
            or row.get("exit_observation_semantics")
            != "BOOK_EXECUTABLE_NOT_FILL"
            or not _close(proceeds, quantity * exit_vwap - exit_fee)
            or completion_ts > close_ts
        ):
            return None
        completion_cash = proceeds
    else:
        payout = _finite(row.get("payout"))
        settlement_ts = _finite(row.get("settlement_timestamp_ms"))
        settlement_price = _finite(row.get("settlement_price"))
        price_to_beat = _finite(row.get("price_to_beat"))
        winning_outcome = row.get("winning_outcome")
        outcome = row.get("outcome")
        calculated_winner = (
            "Up"
            if settlement_price is not None
            and price_to_beat is not None
            and settlement_price >= price_to_beat
            else "Down"
        )
        if (
            payout is None
            or outcome not in {"Up", "Down"}
            or winning_outcome not in {"Up", "Down"}
            or settlement_price is None
            or settlement_price <= 0
            or price_to_beat is None
            or price_to_beat <= 0
            or winning_outcome != calculated_winner
            or not _close(
                payout,
                target_size if outcome == winning_outcome else 0.0,
            )
            or completion_ts < close_ts
            or settlement_ts is None
            or not close_ts * 1000 <= settlement_ts <= close_ts * 1000 + 10_000
            or not isinstance(row.get("settlement_source"), str)
            or not row["settlement_source"].strip()
            or row.get("settlement_source_verified") is not True
        ):
            return None
        completion_cash = payout

    pnl = completion_cash - entry_cost
    recorded_pnl = _finite(row.get("realized_simulated_pnl"))
    net_pnl = _finite(row.get("net_pnl_usd"))
    net_return = _finite(row.get("net_return_per_dollar_risked"))
    if (
        recorded_pnl is None
        or net_pnl is None
        or net_return is None
        or not _close(recorded_pnl, pnl)
        or not _close(net_pnl, pnl)
        or not _close(net_return, pnl / entry_cost)
    ):
        return None
    return {
        "event_id": row["event_id"],
        "market_id": row["market_id"],
        "cohort_key": row["profitability_cohort_key"],
        "entry_ts": entry_ts,
        "close_ts": close_ts,
        "completion_ts": completion_ts,
        "net_pnl_usd": pnl,
        "net_return_per_dollar_risked": net_return,
    }


def _maximum_drawdown(rows):
    equity = 0.0
    peak = 0.0
    maximum = 0.0
    for row in rows:
        equity += row["net_pnl_usd"]
        peak = max(peak, equity)
        maximum = max(maximum, peak - equity)
    return maximum


def build_profitability_acceptance(
    execution_path: Path,
    gate_path: Path,
    state_path: Path,
    now: float = None,
) -> dict:
    current = time.time() if now is None else _finite(now)
    if current is None:
        return _configuration_error(0.0, "acceptance_time_invalid")

    gate, gate_reason = load_profitability_gate(gate_path, current)
    if gate_reason:
        return _configuration_error(current, gate_reason)

    configured_gate_path = os.getenv("PROFITABILITY_GATE_PATH")
    if (
        configured_gate_path
        and Path(configured_gate_path).resolve() != Path(gate_path).resolve()
    ):
        return _configuration_error(
            current, "profitability_gate_path_mismatch", gate=gate,
        )
    if os.getenv("PROFITABILITY_GATE_ENABLE", "0").strip().lower() not in {
        "1", "true", "yes", "on",
    }:
        return _configuration_error(
            current, "profitability_gate_not_enabled", gate=gate,
        )

    snapshot_path = Path(os.getenv(
        "PROBABILITY_VALIDATION_CALIBRATION_PATH",
        "data/probability-calibration-validation.json",
    ))
    _, snapshot_reason = load_frozen_calibration_snapshot(
        snapshot_path,
        current,
        expected_content_hash=gate["calibration_snapshot_hash"],
    )
    if snapshot_reason:
        return _configuration_error(current, snapshot_reason, gate=gate)

    if (
        os.getenv("PROFITABILITY_COHORT_VERSION", "1")
        != gate["profitability_cohort_version"]
    ):
        return _configuration_error(
            current, "profitability_cohort_version_mismatch", gate=gate,
        )
    if os.getenv("SHADOW_CASH_LEDGER_VERSION", "2") != "2":
        return _configuration_error(
            current, "cash_ledger_version_mismatch", gate=gate,
        )
    for strategy, expected in gate["target_base_config_hashes"].items():
        if canonical_strategy_base_config_hash(strategy) != expected:
            return _configuration_error(
                current, "strategy_base_config_hash_mismatch", gate=gate,
            )

    starting_capital = _finite(os.getenv("SHADOW_SIZING_CAPITAL_USD", "1000"))
    if starting_capital is None or starting_capital <= 0:
        return _configuration_error(
            current, "starting_shadow_capital_invalid", gate=gate,
        )

    state, state_reason = _read_state(state_path)
    if state_reason:
        return _configuration_error(current, state_reason, gate=gate)
    state_counters = {field: state.get(field) for field in _REAL_ORDER_FIELDS}
    if any(type(value) is not int for value in state_counters.values()):
        return _configuration_error(
            current, "strategy_state_invalid", gate=gate,
        )
    if any(value != 0 for value in state_counters.values()):
        return _result(
            now=current,
            status="FAIL",
            classification="SAFETY_FAILURE",
            reason="real_order_invariant",
            exit_code=1,
            gate=gate,
            metrics=_empty_metrics(starting_capital),
            real_orders=state_counters,
        )
    if (
        state.get("calibration_mode") is not False
        or state.get("portfolio_limits_enforced") is not True
        or state.get("risk_mode") != "PORTFOLIO_LIMITS_ENFORCED"
    ):
        return _configuration_error(
            current,
            "strategy_state_not_portfolio_limited",
            gate=gate,
            metrics=_empty_metrics(starting_capital),
            real_orders=state_counters,
        )

    rows, source, source_reason = _source_rows(execution_path)
    if rows is None:
        return _configuration_error(
            current,
            source_reason,
            gate=gate,
            metrics=_empty_metrics(starting_capital),
            real_orders=state_counters,
        )
    if source_reason:
        return _result(
            now=current,
            status="FAIL",
            classification="DATA_INTEGRITY_FAILURE",
            reason=source_reason,
            exit_code=1,
            gate=gate,
            metrics=_empty_metrics(starting_capital),
            source=source,
            real_orders=state_counters,
        )
    real_order_reason = _real_order_reason(rows)
    if real_order_reason:
        return _result(
            now=current,
            status="FAIL",
            classification=(
                "SAFETY_FAILURE"
                if real_order_reason == "real_order_invariant"
                else "DATA_INTEGRITY_FAILURE"
            ),
            reason=real_order_reason,
            exit_code=1,
            gate=gate,
            metrics=_empty_metrics(starting_capital),
            source=source,
            real_orders=state_counters,
        )

    excluded = Counter()
    candidates = []
    seen_events = {}
    for row in rows:
        if row.get("event_type") != "shadow_complete":
            excluded["unrelated_event"] += 1
            continue
        if row.get("strategy") not in PROBABILITY_STRATEGIES:
            excluded["unrelated_strategy"] += 1
            continue
        if row.get("deployable_pnl") is not True:
            excluded["deployable_pnl_not_true"] += 1
            continue
        entry_ts = _finite(row.get("entry_ts"))
        if entry_ts is None:
            return _result(
                now=current,
                status="FAIL",
                classification="DATA_INTEGRITY_FAILURE",
                reason="ledger_corrupt",
                exit_code=1,
                gate=gate,
                metrics=_empty_metrics(starting_capital),
                excluded=excluded,
                source=source,
                real_orders=state_counters,
            )
        if entry_ts < gate["validation_activated_at"]:
            excluded["pre_validation_window"] += 1
            continue
        identity_reason = _identity_reason(row, gate)
        if identity_reason:
            return _configuration_error(
                current,
                identity_reason,
                gate=gate,
                metrics=_empty_metrics(starting_capital),
                excluded=excluded,
                source=source,
                real_orders=state_counters,
            )
        event_id = row.get("event_id")
        if event_id in seen_events:
            if seen_events[event_id] != row:
                return _result(
                    now=current,
                    status="FAIL",
                    classification="DATA_INTEGRITY_FAILURE",
                    reason="ledger_corrupt",
                    exit_code=1,
                    gate=gate,
                    metrics=_empty_metrics(starting_capital),
                    excluded=excluded,
                    source=source,
                    real_orders=state_counters,
                )
            excluded["duplicate_event"] += 1
            continue
        seen_events[event_id] = row
        trade = _ledger_trade(row, current)
        if trade is None:
            return _result(
                now=current,
                status="FAIL",
                classification="DATA_INTEGRITY_FAILURE",
                reason="ledger_corrupt",
                exit_code=1,
                gate=gate,
                metrics=_empty_metrics(starting_capital),
                excluded=excluded,
                source=source,
                real_orders=state_counters,
            )
        candidates.append(trade)

    trades = []
    claimed_markets = set()
    for trade in sorted(
        candidates,
        key=lambda row: (
            row["entry_ts"], row["event_id"], row["market_id"],
        ),
    ):
        if trade["market_id"] in claimed_markets:
            excluded["duplicate_market"] += 1
            continue
        claimed_markets.add(trade["market_id"])
        trades.append(trade)
    trades.sort(key=lambda row: (
        row["completion_ts"], row["market_id"], row["event_id"],
    ))

    sample_counts = {
        key: 0 for key in gate["eligible_cohorts"]
    }
    for trade in trades:
        sample_counts[trade["cohort_key"]] += 1
    total_pnl = sum(row["net_pnl_usd"] for row in trades)
    mean_return = (
        statistics.fmean(
            row["net_return_per_dollar_risked"] for row in trades
        )
        if trades else None
    )
    maximum_drawdown = _maximum_drawdown(trades)
    lower_bound = block_bootstrap_lower_bound(
        trades,
        gate["profitability_cohort_version"] + "|" + gate["content_hash"],
    )
    runtime = current - gate["validation_activated_at"]
    metrics = {
        "runtime_seconds": runtime,
        "independent_markets": len(trades),
        "completed": len(trades),
        "sample_count": len(trades),
        "total_pnl": total_pnl,
        "total_pnl_usd": total_pnl,
        "mean_return": mean_return,
        "mean_net_return": mean_return,
        "mean_net_return_per_dollar_risked": mean_return,
        "maximum_drawdown_usd": maximum_drawdown,
        "maximum_drawdown_pct": maximum_drawdown / starting_capital,
        "lower_bound_95": lower_bound,
        "confidence_lower_bound_95": lower_bound,
        "starting_shadow_capital_usd": starting_capital,
    }

    def classified(status, classification, reason, exit_code):
        return _result(
            now=current,
            status=status,
            classification=classification,
            reason=reason,
            exit_code=exit_code,
            gate=gate,
            metrics=metrics,
            sample_counts=sample_counts,
            excluded=excluded,
            source=source,
            real_orders=state_counters,
        )

    if len(trades) >= MINIMUM_INDEPENDENT_MARKETS and total_pnl <= 0:
        return classified(
            "FAIL", "ECONOMIC_FAILURE", "total_pnl_not_positive", 1,
        )
    if (
        len(trades) >= MINIMUM_INDEPENDENT_MARKETS
        and (mean_return is None or mean_return <= 0)
    ):
        return classified(
            "FAIL", "ECONOMIC_FAILURE", "mean_net_return_not_positive", 1,
        )
    if maximum_drawdown / starting_capital > MAXIMUM_DRAWDOWN_FRACTION:
        return classified(
            "FAIL", "ECONOMIC_FAILURE", "maximum_drawdown_exceeded", 1,
        )
    if runtime < MINIMUM_RUNTIME_SECONDS:
        return classified(
            "INCOMPLETE", "INSUFFICIENT_EVIDENCE",
            "minimum_runtime_not_met", 2,
        )
    if len(trades) < MINIMUM_INDEPENDENT_MARKETS:
        return classified(
            "INCOMPLETE", "INSUFFICIENT_EVIDENCE",
            "minimum_independent_markets_not_met", 2,
        )
    if not gate["eligible_cohorts"]:
        return classified(
            "INCOMPLETE", "INSUFFICIENT_EVIDENCE",
            "no_eligible_cohorts", 2,
        )
    if any(count < MINIMUM_COHORT_MARKETS for count in sample_counts.values()):
        return classified(
            "INCOMPLETE", "INSUFFICIENT_EVIDENCE",
            "minimum_cohort_samples_not_met", 2,
        )
    if lower_bound is None or lower_bound <= 0:
        return classified(
            "INCOMPLETE", "INSUFFICIENT_CONFIDENCE",
            "lower_bound_95_not_positive", 2,
        )
    return classified(
        "PASS", "SHADOW_EVIDENCE", "profitability_validation_passed", 0,
    )


def _atomic_publish(payload, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def load_profitability_acceptance(
    path: Path,
    now: float,
    expected_gate_hash: str = None,
    expected_snapshot_hash: str = None,
):
    """Load an acceptance artifact without trusting its claimed status."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "profitability_acceptance_not_run"
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "profitability_acceptance_invalid"
    current = _finite(now)
    if (
        current is None
        or not isinstance(payload, dict)
        or set(payload) != _ACCEPTANCE_FIELDS
        or payload.get("version") != ACCEPTANCE_VERSION
    ):
        return None, "profitability_acceptance_invalid"
    if (
        not _valid_hash(payload.get("content_hash"))
        or payload["content_hash"] != canonical_payload_hash(payload)
    ):
        return None, "profitability_acceptance_hash_mismatch"
    status = payload.get("status")
    classification = payload.get("classification")
    exit_code = payload.get("exit_code")
    if (
        status not in {"PASS", "FAIL", "INCOMPLETE"}
        or classification not in {
            "SHADOW_EVIDENCE",
            "ECONOMIC_FAILURE",
            "SAFETY_FAILURE",
            "DATA_INTEGRITY_FAILURE",
            "INSUFFICIENT_EVIDENCE",
            "INSUFFICIENT_CONFIDENCE",
            "CONFIGURATION_ERROR",
        }
        or type(exit_code) is not int
        or (status == "PASS" and exit_code != 0)
        or (status == "FAIL" and exit_code != 1)
        or (status == "INCOMPLETE" and exit_code not in {2, 3})
    ):
        return None, "profitability_acceptance_invalid"
    generated = _finite(payload.get("generated_at"))
    identities = payload.get("identities")
    metrics = payload.get("metrics")
    sample_counts = payload.get("sample_counts")
    if (
        generated is None
        or generated > current
        or not isinstance(identities, dict)
        or not isinstance(metrics, dict)
        or not isinstance(sample_counts, dict)
        or not isinstance(payload.get("enabled_cohorts"), dict)
        or not isinstance(payload.get("excluded"), dict)
        or not isinstance(payload.get("source"), dict)
        or not isinstance(payload.get("checks"), list)
    ):
        return None, "profitability_acceptance_invalid"
    gate_hash = identities.get("profitability_gate_hash")
    snapshot_hash = identities.get("calibration_snapshot_hash")
    if expected_gate_hash is not None and gate_hash != expected_gate_hash:
        return None, "profitability_acceptance_identity_mismatch"
    if (
        expected_snapshot_hash is not None
        and snapshot_hash != expected_snapshot_hash
    ):
        return None, "profitability_acceptance_identity_mismatch"
    for field in _REAL_ORDER_FIELDS:
        value = payload.get(field)
        if classification == "CONFIGURATION_ERROR" and value is None:
            continue
        if type(value) is not int or value != 0:
            return None, "profitability_acceptance_invalid"
    if status == "PASS":
        runtime = _finite(metrics.get("runtime_seconds"))
        markets = metrics.get("independent_markets")
        total_pnl = _finite(metrics.get("total_pnl_usd"))
        mean_return = _finite(
            metrics.get("mean_net_return_per_dollar_risked")
        )
        drawdown = _finite(metrics.get("maximum_drawdown_pct"))
        lower_bound = _finite(metrics.get("lower_bound_95"))
        activated = _finite(payload.get("validation_started_at"))
        expires = _finite(payload.get("validation_expires_at"))
        enabled_cohorts = payload["enabled_cohorts"]
        invalid_sample_counts = any(
            type(count) is not int or count < MINIMUM_COHORT_MARKETS
            for count in sample_counts.values()
        )
        if (
            classification != "SHADOW_EVIDENCE"
            or payload.get("reason") != "profitability_validation_passed"
            or not _valid_hash(gate_hash)
            or not _valid_hash(snapshot_hash)
            or activated is None
            or expires is None
            or not activated <= generated < expires
            or current >= expires
            or runtime is None
            or runtime < MINIMUM_RUNTIME_SECONDS
            or type(markets) is not int
            or markets < MINIMUM_INDEPENDENT_MARKETS
            or total_pnl is None
            or total_pnl <= 0
            or mean_return is None
            or mean_return <= 0
            or drawdown is None
            or not 0 <= drawdown <= MAXIMUM_DRAWDOWN_FRACTION
            or lower_bound is None
            or lower_bound <= 0
            or not sample_counts
            or set(sample_counts) != set(enabled_cohorts)
            or invalid_sample_counts
            or sum(sample_counts.values()) != markets
        ):
            return None, "profitability_acceptance_invalid"
    return payload, None


def run(
    execution_path: Path,
    gate_path: Path,
    state_path: Path,
    output_path: Path,
    now: float = None,
) -> int:
    snapshot_path = Path(os.getenv(
        "PROBABILITY_VALIDATION_CALIBRATION_PATH",
        "data/probability-calibration-validation.json",
    ))
    protected_names = {
        ".env",
        "env.example",
        "live_markets.json",
        "probability-calibration-map.json",
        "probability-calibration-research.json",
        "probability-calibration-validation.json",
        "profitability-gates.json",
    }
    resolved_output = Path(output_path).resolve()
    resolved_inputs = {
        Path(gate_path).resolve(),
        Path(state_path).resolve(),
        snapshot_path.resolve(),
        *(
            Path(candidate).resolve()
            for candidate in history_paths(Path(execution_path))
        ),
    }
    if (
        resolved_output in resolved_inputs
        or Path(output_path).name.lower() in protected_names
    ):
        result = _configuration_error(
            time.time() if now is None else now,
            "acceptance_output_path_conflict",
        )
        print(json.dumps(result, sort_keys=True))
        return 3
    result = build_profitability_acceptance(
        execution_path, gate_path, state_path, now=now,
    )
    try:
        _atomic_publish(result, output_path)
    except OSError:
        result = _configuration_error(
            time.time() if now is None else now,
            "acceptance_output_write_error",
            gate=None,
        )
    print(json.dumps(result, sort_keys=True))
    return int(result["exit_code"])
