import json
import math
import os
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
                if (row.get("event_type") == "shadow_prediction_complete" and
                        row.get("strategy") in STRATEGIES):
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
        config_hashes = {
            strategy: max(
                (row for row in all_rows if row.get("strategy") == strategy),
                key=lambda row: float(row.get("ts", 0)), default={},
            ).get("strategy_config_hash")
            for strategy in STRATEGIES
        }
        rows = [row for row in all_rows if
                row.get("strategy_config_hash") == config_hashes.get(row.get("strategy"))]
        selected = {value for value in config_hashes.values()}
        config_hash = next(iter(selected)) if len(selected) == 1 else "latest_by_strategy"
    else:
        config_hashes = {strategy: config_hash for strategy in STRATEGIES}
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
        "outcome", "close_ts", "estimated_probability", "raw_estimated_probability",
        "probability_model_id", "expected_fill_price", "net_ev",
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
        "config_hashes": config_hashes,
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


def _probability_metrics(rows):
    if not rows:
        return {
            "samples": 0, "expected_up_rate": None, "realized_up_rate": None,
            "brier_score": None, "log_loss": None, "origin_accepted": 0,
            "origin_rejected": 0, "market_implied_samples": 0,
            "market_implied_brier_score": None, "market_implied_log_loss": None,
            "brier_skill_vs_market": None, "calibration_buckets": {},
            "independent_markets": 0, "independent_close_windows": 0,
        }
    buckets = defaultdict(list)
    probabilities = []
    outcomes = []
    for row in rows:
        probability = float(row["estimated_up_probability"])
        actual = int(row["actual_up"])
        probabilities.append(probability)
        outcomes.append(actual)
        buckets[min(9, int(probability * 10))].append((probability, actual))
    calibration = {
        f"{bucket / 10:.1f}-{(bucket + 1) / 10:.1f}": {
            "samples": len(values),
            "expected_up_rate": sum(value[0] for value in values) / len(values),
            "realized_up_rate": sum(value[1] for value in values) / len(values),
        }
        for bucket, values in sorted(buckets.items())
    }
    market_rows = [row for row in rows if row.get("market_implied_up_probability") is not None]
    market_brier = market_log_loss = None
    if market_rows:
        market_brier = sum(
            (float(row["market_implied_up_probability"]) - int(row["actual_up"])) ** 2
            for row in market_rows
        ) / len(market_rows)
        market_log_loss = sum(
            -(
                int(row["actual_up"]) * math.log(min(1 - 1e-12, max(
                    1e-12, float(row["market_implied_up_probability"]),
                )))
                + (1 - int(row["actual_up"])) * math.log(min(1 - 1e-12, max(
                    1e-12, 1 - float(row["market_implied_up_probability"]),
                )))
            )
            for row in market_rows
        ) / len(market_rows)
    model_brier = sum(
        (probability - actual) ** 2
        for probability, actual in zip(probabilities, outcomes)
    ) / len(rows)
    return {
        "samples": len(rows),
        "expected_up_rate": sum(probabilities) / len(rows),
        "realized_up_rate": sum(outcomes) / len(rows),
        "brier_score": round(model_brier, 12),
        "log_loss": round(sum(float(row["log_loss"]) for row in rows) / len(rows), 12),
        "uninformative_brier_score": 0.25,
        "uninformative_log_loss": round(math.log(2), 12),
        "market_implied_samples": len(market_rows),
        "market_implied_brier_score": round(market_brier, 12) if market_brier is not None else None,
        "market_implied_log_loss": round(market_log_loss, 12) if market_log_loss is not None else None,
        "brier_skill_vs_market": (
            round(1 - model_brier / market_brier, 12)
            if market_brier is not None and market_brier > 0 else None
        ),
        "origin_accepted": sum(row.get("origin_decision") == "ACCEPT" for row in rows),
        "origin_rejected": sum(row.get("origin_decision") != "ACCEPT" for row in rows),
        "calibration_buckets": calibration,
        "independent_markets": len({row.get("market_id") for row in rows}),
        "independent_close_windows": len({row.get("close_ts") for row in rows}),
    }


def _sufficiency(rows, metrics):
    minimum_samples = int(os.getenv("CALIBRATION_MIN_SAMPLES", "500"))
    minimum_close_windows = int(os.getenv("CALIBRATION_MIN_CLOSE_WINDOWS", "100"))
    minimum_bucket_samples = int(os.getenv("CALIBRATION_MIN_BUCKET_SAMPLES", "30"))
    bucket_counts = [bucket["samples"] for bucket in metrics["calibration_buckets"].values()]
    smallest_bucket = min(bucket_counts) if bucket_counts else 0
    enough_core = (
        len(rows) >= minimum_samples
        and metrics["independent_close_windows"] >= minimum_close_windows
    )
    status = (
        "CALIBRATION_READY"
        if enough_core and smallest_bucket >= minimum_bucket_samples
        else "PRELIMINARY"
        if enough_core
        else "INSUFFICIENT_DATA"
    )
    return {
        "status": status,
        "minimum_samples": minimum_samples,
        "minimum_close_windows": minimum_close_windows,
        "minimum_bucket_samples": minimum_bucket_samples,
        "smallest_occupied_bucket_samples": smallest_bucket,
    }


def build_probability_calibration(path, config_hash=None):
    by_id = {}
    duplicates = 0
    for row in _prediction_rows(path):
        event_id = row.get("event_id")
        if event_id and event_id in by_id:
            duplicates += 1
            continue
        by_id[event_id or f"legacy:{len(by_id)}"] = row
    all_rows = [row for row in by_id.values() if all(
        row.get(field) is not None
        for field in ("estimated_up_probability", "actual_up", "log_loss")
    )]
    if config_hash in {None, "latest"}:
        config_hashes = {
            strategy: max(
                (row for row in all_rows if row.get("strategy") == strategy),
                key=lambda row: float(row.get("ts", 0)), default={},
            ).get("strategy_config_hash")
            for strategy in STRATEGIES
        }
        rows = [row for row in all_rows if
                row.get("strategy_config_hash") == config_hashes.get(row.get("strategy"))]
        selected = {value for value in config_hashes.values() if value is not None}
        selected_hash = next(iter(selected)) if len(selected) == 1 else "latest_by_strategy"
    else:
        config_hashes = {strategy: config_hash for strategy in STRATEGIES}
        rows = [row for row in all_rows if row.get("strategy_config_hash") == config_hash]
        selected_hash = config_hash
    by_strategy = {}
    for strategy in sorted(STRATEGIES):
        strategy_rows = [row for row in rows if row.get("strategy") == strategy]
        metrics = _probability_metrics(strategy_rows)
        metrics["sufficiency"] = _sufficiency(strategy_rows, metrics)
        by_strategy[strategy] = metrics
    strategy_rows = {
        strategy: [row for row in rows if row.get("strategy") == strategy]
        for strategy in sorted(STRATEGIES)
    }
    by_strategy_asset = {
        strategy: {
            asset: _probability_metrics([row for row in selected if row.get("asset") == asset])
            for asset in sorted({row.get("asset") for row in selected if row.get("asset")})
        }
        for strategy, selected in strategy_rows.items()
    }
    by_strategy_timeframe = {
        strategy: {
            timeframe: _probability_metrics([
                row for row in selected if row.get("timeframe") == timeframe
            ])
            for timeframe in sorted({
                row.get("timeframe") for row in selected if row.get("timeframe")
            })
        }
        for strategy, selected in strategy_rows.items()
    }
    by_strategy_asset_timeframe = {
        strategy: {
            asset: {
                timeframe: _probability_metrics([
                    row for row in selected
                    if row.get("asset") == asset and row.get("timeframe") == timeframe
                ])
                for timeframe in sorted({
                    row.get("timeframe") for row in selected
                    if row.get("asset") == asset and row.get("timeframe")
                })
            }
            for asset in sorted({row.get("asset") for row in selected if row.get("asset")})
        }
        for strategy, selected in strategy_rows.items()
    }
    statuses = [metrics["sufficiency"]["status"] for metrics in by_strategy.values()]
    calibration_status = (
        "CALIBRATION_READY" if statuses and all(status == "CALIBRATION_READY" for status in statuses)
        else "PRELIMINARY" if statuses and all(status != "INSUFFICIENT_DATA" for status in statuses)
        else "INSUFFICIENT_DATA"
    )
    return {
        "config_hash": selected_hash,
        "config_hashes": config_hashes,
        "sample_count": len(rows),
        "excluded_other_config": len(all_rows) - len(rows),
        "duplicate_events": duplicates,
        "independent_markets": len({row.get("market_id") for row in rows}),
        "independent_close_windows": len({row.get("close_ts") for row in rows}),
        "probability_model_ids": dict(Counter(
            row.get("probability_model_id") or "<missing>" for row in rows
        )),
        "calibration_status": calibration_status,
        "sufficiency": {
            **{
                key: value for key, value in
                by_strategy["late_window_directional_ev"]["sufficiency"].items()
                if key != "status"
            },
            "status": calibration_status,
            "by_strategy": {
                strategy: metrics["sufficiency"]["status"]
                for strategy, metrics in by_strategy.items()
            },
        },
        "by_strategy": by_strategy,
        "by_strategy_asset": by_strategy_asset,
        "by_strategy_timeframe": by_strategy_timeframe,
        "by_strategy_asset_timeframe": by_strategy_asset_timeframe,
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


def probability_main(path="logs/shadow-execution.jsonl", config_hash="latest"):
    report = build_probability_calibration(Path(path), config_hash)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["sample_count"] else 2
