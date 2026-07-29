import hashlib
import json
import math
import random
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from .jsonl_history import history_paths, open_history


PROBABILITY_STRATEGIES = frozenset({
    "late_window_directional_ev",
    "low_price_lottery_ev",
})
BLOCKING_EXCLUSIONS = frozenset({
    "invalid_json",
    "missing_event_id",
    "missing_entry_event",
    "strategy_config_mismatch",
    "probability_model_mismatch",
    "outcome_mismatch",
    "fee_schedule_unavailable",
    "pnl_recalculation_mismatch",
    "real_order_invariant",
})
SECONDS_BINS = (0, 30, 60, 90, 180, 300, 600, float("inf"))
SETTLEMENT_MAX_DELAY_MS = 10_000
_REAL_ORDER_FIELDS = (
    "real_order_submissions",
    "real_orders",
    "real_fills",
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


def _row_timestamp(row):
    value = _finite(row.get("ts", row.get("timestamp")))
    return value if value is not None else float("-inf")


def _source_rows(path):
    path = Path(path)
    rows = []
    excluded = Counter()
    files = []
    for candidate in history_paths(path):
        candidate = Path(candidate)
        if not candidate.exists():
            continue
        try:
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
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        excluded["invalid_json"] += 1
                        continue
                    if not isinstance(row, dict):
                        excluded["invalid_json"] += 1
                        continue
                    rows.append(row)
        except UnicodeError:
            excluded["invalid_json"] += 1
        except (OSError, EOFError):
            excluded["source_read_error"] += 1
            excluded["invalid_json"] += 1
    if not files:
        excluded["source_file_missing"] += 1
    return rows, excluded, {
        "path": str(path),
        "files": files,
    }


def _has_zero_real_orders(row):
    return all(
        field in row
        and not isinstance(row[field], bool)
        and _finite(row[field]) == 0
        for field in _REAL_ORDER_FIELDS
    )


def _entry_index(rows, excluded):
    entries = {}
    for row in rows:
        if row.get("event_type") != "shadow_eval":
            continue
        if row.get("strategy") not in PROBABILITY_STRATEGIES:
            excluded["unrelated_strategy"] += 1
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            excluded["missing_event_id"] += 1
            continue
        if event_id in entries:
            excluded["duplicate_event"] += 1
            continue
        entries[event_id] = row
    return entries


def _completion_rows(rows, excluded):
    completions = []
    seen = set()
    for row in rows:
        if row.get("event_type") != "shadow_complete":
            continue
        if row.get("strategy") not in PROBABILITY_STRATEGIES:
            excluded["unrelated_strategy"] += 1
            continue
        event_id = row.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            excluded["missing_event_id"] += 1
            continue
        if event_id in seen:
            excluded["duplicate_event"] += 1
            continue
        seen.add(event_id)
        completions.append(row)
    return completions


def _selected_hashes(completions, requested):
    if requested is not None:
        return {
            strategy: str(value)
            for strategy, value in requested.items()
            if strategy in PROBABILITY_STRATEGIES and value
        }
    latest = {}
    for row in completions:
        strategy = row.get("strategy")
        config_hash = row.get("strategy_config_hash")
        if not config_hash:
            continue
        key = (
            _row_timestamp(row),
            str(row.get("entry_event_id", "")),
            str(row.get("event_id", "")),
        )
        if strategy not in latest or key > latest[strategy][0]:
            latest[strategy] = (key, str(config_hash))
    return {
        strategy: item[1]
        for strategy, item in sorted(latest.items())
    }


def _identity_reason(entry, complete):
    if entry.get("decision") != "ACCEPT":
        return "missing_entry_event"
    if entry.get("config_version") not in {
        "shadow-buy-rules-v9",
        "shadow-buy-rules-v10",
    }:
        return "strategy_config_mismatch"
    for field in ("strategy", "market_id", "condition_id"):
        if not entry.get(field) or entry.get(field) != complete.get(field):
            return "strategy_config_mismatch"
    if entry.get("config_hash") != complete.get("strategy_config_hash"):
        return "strategy_config_mismatch"
    if (
        not entry.get("probability_model_id")
        or entry.get("probability_model_id")
        != complete.get("probability_model_id")
    ):
        return "probability_model_mismatch"
    if not entry.get("outcome") or entry.get("outcome") != complete.get("outcome"):
        return "outcome_mismatch"
    for field in ("asset", "timeframe"):
        if not entry.get(field) or entry.get(field) != complete.get(field):
            return "strategy_config_mismatch"
    if not _has_zero_real_orders(entry) or not _has_zero_real_orders(complete):
        return "real_order_invariant"
    return None


def _entry_cash(entry):
    fee_rate = _finite(entry.get("fee_rate"))
    fee = _finite(entry.get("dynamic_fee"))
    if (
        fee_rate is None
        or not 0 <= fee_rate <= 1
        or fee is None
        or fee < 0
        or (fee_rate > 0 and fee <= 0)
        or (fee_rate == 0 and fee != 0)
    ):
        return None, "fee_schedule_unavailable"

    size = _finite(entry.get("dynamic_target_size"))
    completion_size = _finite(entry.get("target_size"))
    depth = _finite(entry.get("executable_depth_size"))
    vwap = _finite(entry.get("dynamic_vwap"))
    notional = _finite(entry.get("dynamic_buy_notional"))
    if (
        entry.get("sizing_mode") != "real_market_dynamic_v1"
        or entry.get("target_depth_ok") is not True
        or size is None
        or completion_size is None
        or depth is None
        or vwap is None
        or notional is None
        or size <= 0
        or not _close(size, completion_size)
        or depth + 1e-9 < size
        or not 0 < vwap < 1
        or notional <= 0
        or not _close(notional, size * vwap)
        or fee > fee_rate * notional + 0.001
    ):
        return None, "pnl_recalculation_mismatch"

    cash = notional + fee
    buffer_value = _finite(entry.get("dynamic_buffer"))
    if buffer_value is None or buffer_value < 0:
        return None, "pnl_recalculation_mismatch"
    all_in = _finite(entry.get("dynamic_all_in_cost"))
    if all_in is not None and not _close(all_in, cash + buffer_value):
        return None, "pnl_recalculation_mismatch"
    if entry.get("config_version") == "shadow-buy-rules-v10":
        dynamic_cash = _finite(entry.get("dynamic_cash_cost"))
        risk_adjusted = _finite(entry.get("dynamic_risk_adjusted_cost"))
        maximum_loss = _finite(entry.get("dynamic_maximum_loss"))
        if (
            dynamic_cash is None
            or risk_adjusted is None
            or maximum_loss is None
            or not _close(dynamic_cash, cash)
            or not _close(risk_adjusted, cash + buffer_value)
            or not _close(maximum_loss, cash)
        ):
            return None, "pnl_recalculation_mismatch"
    return {
        "target_size": size,
        "entry_cash_usd": cash,
        "dynamic_vwap": vwap,
        "dynamic_buy_notional": notional,
        "dynamic_fee": fee,
        "fee_rate": fee_rate,
    }, None


def _completion_cash(
    complete,
    target_size,
    entry_cash,
    legacy,
    price_to_beat,
    close_ts,
):
    exit_completion = (
        complete.get("completion_reason") == "profit_target_book_executable"
        or complete.get("exit_vwap") is not None
    )
    if exit_completion:
        quantity = _finite(complete.get("exit_fill_quantity"))
        vwap = _finite(complete.get("exit_vwap"))
        fee = _finite(complete.get("exit_total_fee"))
        if (
            quantity is None
            or vwap is None
            or fee is None
            or not _close(quantity, target_size)
            or not 0 < vwap < 1
            or fee < 0
            or fee > quantity * vwap
            or complete.get("exit_depth_ok") is not True
            or complete.get("exit_book_fresh") is not True
            or complete.get("exit_observation_semantics")
            != "BOOK_EXECUTABLE_NOT_FILL"
        ):
            return None, "pnl_recalculation_mismatch"
        cash = quantity * vwap - fee
        explicit_cash = _finite(complete.get("exit_cash_proceeds"))
        if not legacy and (
            explicit_cash is None or not _close(explicit_cash, cash)
        ):
            return None, "pnl_recalculation_mismatch"
        completion_type = "profit_exit"
    else:
        payout = _finite(complete.get("payout"))
        settlement_price = _finite(complete.get("settlement_price"))
        settlement_timestamp = _finite(complete.get("settlement_timestamp_ms"))
        claimed_winner = complete.get("winning_outcome")
        winning_outcome = (
            "Up" if settlement_price is not None
            and settlement_price >= price_to_beat else "Down"
        )
        expected_payout = (
            target_size if complete.get("outcome") == winning_outcome else 0.0
        )
        if (
            payout is None
            or payout < 0
            or settlement_price is None
            or settlement_price <= 0
            or settlement_timestamp is None
            or not close_ts * 1000
            <= settlement_timestamp
            <= close_ts * 1000 + SETTLEMENT_MAX_DELAY_MS
            or claimed_winner != winning_outcome
            or not _close(payout, expected_payout)
        ):
            return None, "pnl_recalculation_mismatch"
        cash = payout
        completion_type = "settlement"

    net_pnl = cash - entry_cash
    recorded_pnl = _finite(complete.get("realized_simulated_pnl"))
    if not legacy and (
        recorded_pnl is None or not _close(recorded_pnl, net_pnl)
    ):
        return None, "pnl_recalculation_mismatch"
    return {
        "completion_type": completion_type,
        "completion_cash_usd": cash,
        "net_pnl_usd": net_pnl,
    }, None


def _reconcile_candidate(entry, complete):
    reason = _identity_reason(entry, complete)
    if reason:
        return None, reason
    entry_values, reason = _entry_cash(entry)
    if reason:
        return None, reason

    complete_size = _finite(complete.get("target_size"))
    entry_ts = _finite(entry.get("ts", entry.get("timestamp")))
    close_ts = _finite(complete.get("close_ts"))
    completion_ts = _finite(complete.get("ts", complete.get("timestamp")))
    probability = _finite(entry.get("calibration_input_probability"))
    expected_fill = _finite(entry.get("expected_fill_price"))
    price_to_beat = _finite(entry.get("price_to_beat"))
    seconds_to_close = _finite(entry.get("seconds_to_close"))
    exit_completion = (
        complete.get("completion_reason") == "profit_target_book_executable"
        or complete.get("exit_vwap") is not None
    )
    if (
        complete_size is None
        or not _close(complete_size, entry_values["target_size"])
        or entry_ts is None
        or close_ts is None
        or completion_ts is None
        or probability is None
        or not 0 <= probability <= 1
        or expected_fill is None
        or not 0 <= expected_fill <= 1
        or price_to_beat is None
        or price_to_beat <= 0
        or seconds_to_close is None
        or seconds_to_close < 0
        or close_ts <= entry_ts
        or not seconds_to_close
        <= close_ts - entry_ts
        <= seconds_to_close + 1.000001
        or completion_ts <= entry_ts
        or (exit_completion and completion_ts > close_ts)
        or (not exit_completion and completion_ts < close_ts)
    ):
        return None, "pnl_recalculation_mismatch"

    ledger_version = complete.get("cash_ledger_version")
    legacy = entry.get("config_version") == "shadow-buy-rules-v9"
    if not legacy and ledger_version != 2:
        return None, "pnl_recalculation_mismatch"
    completion_values, reason = _completion_cash(
        complete,
        entry_values["target_size"],
        entry_values["entry_cash_usd"],
        legacy,
        price_to_beat,
        close_ts,
    )
    if reason:
        return None, reason

    trade = {
        "event_id": complete["event_id"],
        "entry_event_id": entry["event_id"],
        "strategy": entry["strategy"],
        "strategy_config_hash": entry["config_hash"],
        "probability_model_id": entry["probability_model_id"],
        "market_id": entry["market_id"],
        "condition_id": entry["condition_id"],
        "asset": entry["asset"],
        "timeframe": entry["timeframe"],
        "outcome": entry["outcome"],
        "entry_ts": entry_ts,
        "close_ts": close_ts,
        "completion_ts": completion_ts,
        "calibration_input_probability": probability,
        "expected_fill_price": expected_fill,
        "price_to_beat": price_to_beat,
        "seconds_to_close": seconds_to_close,
        "cash_ledger_version": 2,
        **entry_values,
        **completion_values,
    }
    trade["net_return_per_dollar_risked"] = (
        trade["net_pnl_usd"] / trade["entry_cash_usd"]
    )
    return trade, None


def reconcile_probability_trades(
    strategy_audit_path: Path,
    execution_path: Path,
    config_hashes: Optional[Dict[str, str]] = None,
) -> dict:
    audit_rows, audit_excluded, audit_source = _source_rows(strategy_audit_path)
    execution_rows, execution_excluded, execution_source = _source_rows(
        execution_path
    )
    excluded = audit_excluded + execution_excluded
    entries = _entry_index(audit_rows, excluded)
    completions = _completion_rows(execution_rows, excluded)
    selected_hashes = _selected_hashes(completions, config_hashes)

    candidates = []
    for complete in completions:
        strategy = complete.get("strategy")
        selected_hash = selected_hashes.get(strategy)
        if (
            selected_hash is not None
            and complete.get("strategy_config_hash") != selected_hash
        ):
            excluded["unselected_config_hash"] += 1
            continue
        if selected_hash is None or not complete.get("strategy_config_hash"):
            excluded["strategy_config_mismatch"] += 1
            continue
        entry_id = complete.get("entry_event_id")
        if not isinstance(entry_id, str) or entry_id not in entries:
            excluded["missing_entry_event"] += 1
            continue
        trade, reason = _reconcile_candidate(entries[entry_id], complete)
        if reason:
            excluded[reason] += 1
        else:
            candidates.append(trade)

    trades = []
    claimed_markets = set()
    for trade in sorted(
        candidates,
        key=lambda row: (
            row["entry_ts"],
            row["entry_event_id"],
            row["event_id"],
        ),
    ):
        if trade["market_id"] in claimed_markets:
            excluded["duplicate_market"] += 1
            continue
        claimed_markets.add(trade["market_id"])
        trades.append(trade)

    return {
        "trades": trades,
        "excluded": dict(sorted(excluded.items())),
        "selected_config_hashes": selected_hashes,
        "source": {
            "strategy_audit": audit_source,
            "execution_log": execution_source,
        },
    }


def _decile(value):
    number = _finite(value)
    if number is None or not 0 <= number <= 1:
        raise ValueError("bucket value must be finite and between 0 and 1")
    index = min(9, int(number * 10))
    return f"{index / 10:.1f}-{(index + 1) / 10:.1f}"


def probability_bucket(value: float) -> str:
    return _decile(value)


def fill_price_bucket(value: float) -> str:
    return _decile(value)


def seconds_to_close_bucket(value: float) -> str:
    number = _finite(value)
    if number is None or number < 0:
        raise ValueError("seconds_to_close must be finite and non-negative")
    for lower, upper in zip(SECONDS_BINS, SECONDS_BINS[1:]):
        if lower <= number < upper:
            upper_label = "inf" if math.isinf(upper) else str(int(upper))
            return f"{int(lower)}-{upper_label}"
    raise ValueError("seconds_to_close is outside configured bins")


def cohort_dimensions(row: dict) -> dict:
    return {
        "strategy": str(row["strategy"]),
        "asset": str(row["asset"]),
        "timeframe": str(row["timeframe"]),
        "outcome": str(row["outcome"]),
        "probability": probability_bucket(
            row["calibration_input_probability"]
        ),
        "fill": fill_price_bucket(row["expected_fill_price"]),
        "seconds": seconds_to_close_bucket(row["seconds_to_close"]),
    }


def cohort_key(row: dict) -> str:
    dimensions = cohort_dimensions(row)
    return "|".join(
        f"{name}={dimensions[name]}"
        for name in (
            "strategy",
            "asset",
            "timeframe",
            "outcome",
            "probability",
            "fill",
            "seconds",
        )
    )


def block_bootstrap_lower_bound(
    rows: List[dict],
    seed_material: str,
    resamples: int = 10_000,
) -> Optional[float]:
    if not rows:
        return None
    if resamples <= 0:
        raise ValueError("resamples must be positive")
    blocks = defaultdict(list)
    for row in rows:
        close_ts = _finite(row.get("close_ts"))
        value = _finite(row.get("net_return_per_dollar_risked"))
        if close_ts is None or close_ts < 0 or value is None:
            return None
        blocks[int(close_ts // 14_400)].append(
            (str(row.get("market_id", "")), value)
        )
    block_ids = sorted(blocks)
    block_values = {
        block_id: [
            value for _, value in sorted(blocks[block_id], key=lambda item: item[0])
        ]
        for block_id in block_ids
    }
    seed = int.from_bytes(
        hashlib.sha256(str(seed_material).encode("utf-8")).digest(),
        "big",
    )
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        sample = []
        for _ in block_ids:
            sample.extend(block_values[rng.choice(block_ids)])
        means.append(statistics.fmean(sample))
    means.sort()
    rank = max(0, math.ceil(0.05 * len(means)) - 1)
    return means[rank]


def largest_positive_market_share(rows: List[dict]) -> Optional[float]:
    positive = [
        value
        for value in (_finite(row.get("net_pnl_usd")) for row in rows)
        if value is not None and value > 0
    ]
    if not positive:
        return None
    return max(positive) / sum(positive)


def aggregate_metrics(rows: List[dict], seed_material: str) -> dict:
    if not rows:
        return {
            "independent_markets": 0,
            "entry_cash_usd": 0.0,
            "completion_cash_usd": 0.0,
            "net_pnl_usd": 0.0,
            "mean_net_return": None,
            "lower_bound_95": None,
            "largest_positive_market_share": None,
        }
    return {
        "independent_markets": len(rows),
        "entry_cash_usd": sum(row["entry_cash_usd"] for row in rows),
        "completion_cash_usd": sum(
            row["completion_cash_usd"] for row in rows
        ),
        "net_pnl_usd": sum(row["net_pnl_usd"] for row in rows),
        "mean_net_return": statistics.fmean(
            row["net_return_per_dollar_risked"] for row in rows
        ),
        "lower_bound_95": block_bootstrap_lower_bound(rows, seed_material),
        "largest_positive_market_share": largest_positive_market_share(rows),
    }


def build_profitability_report(
    strategy_audit_path: Path,
    execution_path: Path,
    config_hashes: Optional[Dict[str, str]] = None,
) -> dict:
    reconciled = reconcile_probability_trades(
        strategy_audit_path,
        execution_path,
        config_hashes,
    )
    trades = reconciled["trades"]
    source = reconciled["source"]
    seed_payload = {
        "version": 1,
        "source": source,
        "selected_config_hashes": reconciled["selected_config_hashes"],
    }
    seed_material = hashlib.sha256(json.dumps(
        seed_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")).hexdigest()
    grouped = defaultdict(list)
    for trade in trades:
        grouped[cohort_key(trade)].append(trade)
    cohorts = {}
    for key in sorted(grouped):
        cohort_rows = grouped[key]
        cohorts[key] = {
            "dimensions": cohort_dimensions(cohort_rows[0]),
            "independent_markets": len(cohort_rows),
            "mean_net_return": statistics.fmean(
                row["net_return_per_dollar_risked"] for row in cohort_rows
            ),
            "net_pnl_usd": sum(row["net_pnl_usd"] for row in cohort_rows),
            "lower_bound_95": block_bootstrap_lower_bound(
                cohort_rows,
                seed_material + "|" + key,
            ),
            "largest_positive_market_share": largest_positive_market_share(
                cohort_rows
            ),
        }
    excluded = reconciled["excluded"]
    generated_at = max(
        (row["completion_ts"] for row in trades),
        default=0.0,
    )
    overall = aggregate_metrics(trades, seed_material)
    return {
        "version": 1,
        "generated_at": generated_at,
        "source": source,
        "selected_config_hashes": reconciled["selected_config_hashes"],
        "independent_markets": len(trades),
        "excluded": excluded,
        "blocking_exclusions": {
            reason: excluded[reason]
            for reason in sorted(BLOCKING_EXCLUSIONS)
            if excluded.get(reason, 0) > 0
        },
        "cash_ledger": {
            "version": 2,
            "basis": "cash_flows_exclude_risk_buffers",
            "entry_cash_usd": overall["entry_cash_usd"],
            "completion_cash_usd": overall["completion_cash_usd"],
            "net_pnl_usd": overall["net_pnl_usd"],
        },
        "overall": overall,
        "cohorts": cohorts,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }
