import json
from collections import Counter, defaultdict
from pathlib import Path

from .http_utils import HttpClient
from .jsonl_history import history_paths, open_history
from .polymarket_data import parse_jsonish


STRATEGIES = {"late_window_directional_ev", "low_price_lottery_ev"}


def _rows(path):
    for history_path in history_paths(path):
        if not history_path.exists():
            continue
        with open_history(history_path) as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("event_type") == "shadow_complete" and row.get("strategy") in STRATEGIES:
                    yield row


def _strategy_metrics(rows):
    complete = [row for row in rows if all(row.get(field) is not None for field in (
        "estimated_probability", "expected_fill_price", "net_ev", "winning_outcome",
    ))]
    if not complete:
        return {"samples": 0, "wins": 0, "realized_hit_rate": None,
                "expected_hit_rate": None, "brier_score": None,
                "average_entry_price": None, "average_net_ev": None,
                "realized_pnl": None, "maximum_drawdown": None,
                "maximum_losing_streak": None, "calibration_buckets": {}}
    outcomes = [1.0 if row.get("outcome") == row.get("winning_outcome") else 0.0 for row in complete]
    probabilities = [float(row["estimated_probability"]) for row in complete]
    equity = peak = max_drawdown = 0.0
    losing_streak = max_losing_streak = 0
    buckets = defaultdict(list)
    for row, probability, outcome in zip(complete, probabilities, outcomes):
        pnl = float(row["realized_simulated_pnl"])
        equity += pnl
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
        losing_streak = losing_streak + 1 if pnl < 0 else 0
        max_losing_streak = max(max_losing_streak, losing_streak)
        bucket = min(9, int(probability * 10))
        buckets[bucket].append(outcome)
    calibration = {
        f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}": {
            "samples": len(values), "realized_hit_rate": sum(values) / len(values),
        }
        for bucket, values in sorted(buckets.items())
    }
    return {
        "samples": len(complete), "wins": int(sum(outcomes)),
        "realized_hit_rate": sum(outcomes) / len(complete),
        "expected_hit_rate": sum(probabilities) / len(complete),
        "brier_score": round(sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(complete), 12),
        "average_entry_price": sum(float(row["expected_fill_price"]) for row in complete) / len(complete),
        "average_net_ev": sum(float(row["net_ev"]) for row in complete) / len(complete),
        "realized_pnl": round(sum(float(row["realized_simulated_pnl"]) for row in complete), 12),
        "maximum_drawdown": round(max_drawdown, 12),
        "maximum_losing_streak": max_losing_streak,
        "calibration_buckets": calibration,
    }


def official_winners(markets):
    winners = {}
    for market in markets:
        outcomes = parse_jsonish(market.get("outcomes")) or []
        prices = parse_jsonish(market.get("outcomePrices")) or []
        if not market.get("closed") or len(outcomes) != 2 or len(prices) != 2:
            continue
        numeric = [float(price) for price in prices]
        if max(numeric) < .99 or min(numeric) > .01 or numeric[0] == numeric[1]:
            continue
        winners[str(market.get("conditionId"))] = str(outcomes[numeric.index(max(numeric))])
    return winners


def fetch_official_winners(condition_ids, base_url="https://gamma-api.polymarket.com", timeout=10):
    ids = sorted({condition_id for condition_id in condition_ids if condition_id})
    winners = {}
    client = HttpClient(timeout=timeout)
    for start in range(0, len(ids), 50):
        batch = ids[start:start + 50]
        response = client.get_json(base_url, "/markets", {
            "condition_ids": batch, "closed": "true", "limit": len(batch),
        })
        winners.update(official_winners(response.data))
    return winners


def build_calibration(path, config_hash=None, resolved_outcomes=None):
    by_id = {}
    duplicates = 0
    for row in _rows(path):
        event_id = row.get("event_id")
        if event_id and event_id in by_id:
            duplicates += 1
            continue
        by_id[event_id or f'legacy:{len(by_id)}'] = row
    all_rows = list(by_id.values())
    if config_hash in {None, "latest"}:
        config_hash = max(all_rows, key=lambda row: float(row.get("ts", 0))).get("strategy_config_hash") if all_rows else None
    rows = [row for row in all_rows if row.get("strategy_config_hash") == config_hash]
    complete_rows = [row for row in rows if all(row.get(field) is not None for field in (
        "estimated_probability", "expected_fill_price", "net_ev", "winning_outcome",
    ))]
    grouped = defaultdict(list)
    mapping_errors = 0
    official_verified = official_mismatches = 0
    for row in rows:
        grouped[(row.get("close_ts"), row.get("outcome"))].append(row)
        price_to_beat = row.get("price_to_beat")
        settlement = row.get("settlement_price")
        if price_to_beat is not None and settlement is not None:
            expected = "Up" if float(settlement) >= float(price_to_beat) else "Down"
            mapping_errors += expected != row.get("winning_outcome")
        official = (resolved_outcomes or {}).get(str(row.get("condition_id")))
        if official is not None:
            official_verified += 1
            official_mismatches += official != row.get("winning_outcome")
    by_strategy = {strategy: _strategy_metrics(
        [row for row in rows if row.get("strategy") == strategy]
    ) for strategy in sorted(STRATEGIES)}
    evidence_fields = (
        "event_id", "entry_event_id", "strategy", "strategy_config_hash", "market_id", "condition_id", "asset", "timeframe",
        "outcome", "close_ts", "estimated_probability", "expected_fill_price", "net_ev",
        "price_to_beat", "consensus_price", "settlement_reference",
        "probability_reference_source", "probability_reference_price",
        "seconds_to_close", "settlement_price",
        "winning_outcome", "realized_simulated_pnl", "model_source", "model_sample_count",
        "model_sample_span_seconds", "minimum_model_sample_span_seconds",
        "volatility_per_sqrt_second", "expected_move_log_std", "reference_log_distance",
        "up_standardized_distance", "up_momentum_z", "up_imbalance_z",
        "up_final_model_z", "paired_book_imbalance", "input_quality_score",
        "confidence_type",
    )
    return {
        "config_hash": config_hash,
        "sample_count": len(rows),
        "complete_model_samples": len(complete_rows),
        "incomplete_model_samples": len(rows) - len(complete_rows),
        "excluded_other_config": len(all_rows) - len(rows),
        "duplicate_completed_events": duplicates,
        "independent_close_windows": len({row.get("close_ts") for row in rows}),
        "correlated_close_outcome_groups": sum(len(group) > 1 for group in grouped.values()),
        "direction_mapping_errors": mapping_errors,
        "direction_mapping_check": "internal_settlement_consistency",
        "official_resolution_verified": official_verified,
        "official_resolution_mismatches": official_mismatches,
        "by_strategy": by_strategy,
        "config_hash_counts": dict(Counter(
            row.get("strategy_config_hash") or "<missing>" for row in all_rows
        )),
        "trades": [{field: row.get(field) for field in evidence_fields} for row in rows],
    }


def main(path="logs/shadow-execution.jsonl", config_hash="latest", verify_official=False,
         gamma_base_url="https://gamma-api.polymarket.com"):
    resolved = None
    if verify_official:
        rows = list(_rows(path))
        resolved = fetch_official_winners(
            [row.get("condition_id") for row in rows], gamma_base_url,
        )
    report = build_calibration(Path(path), config_hash, resolved)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["sample_count"] else 2
