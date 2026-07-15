import json
import gzip

from poly_arb_bot.strategy_calibration import build_calibration, official_winners


def test_calibration_filters_hash_and_detects_correlated_risk(tmp_path):
    path = tmp_path / "execution.jsonl"
    rows = [
        {"event_type": "shadow_complete", "event_id": "a", "ts": 100,
         "strategy": "late_window_directional_ev", "strategy_config_hash": "new",
         "market_id": "m1", "asset": "BTC", "timeframe": "5m", "outcome": "Up",
         "condition_id": "c1",
         "entry_event_id": "entry-a", "volatility_per_sqrt_second": .001,
         "model_sample_span_seconds": 120, "minimum_model_sample_span_seconds": 60,
         "close_ts": 90, "estimated_probability": .8, "expected_fill_price": .6,
         "net_ev": .1, "price_to_beat": 100, "consensus_price": 101,
         "seconds_to_close": 30, "settlement_price": 99, "winning_outcome": "Down",
         "realized_simulated_pnl": -6},
        {"event_type": "shadow_complete", "event_id": "b", "ts": 101,
         "strategy": "low_price_lottery_ev", "strategy_config_hash": "new",
         "market_id": "m2", "asset": "ETH", "timeframe": "5m", "outcome": "Up",
         "condition_id": "c2",
         "close_ts": 90, "estimated_probability": .7, "expected_fill_price": .04,
         "net_ev": .6, "price_to_beat": 10, "consensus_price": 11,
         "seconds_to_close": 20, "settlement_price": 9, "winning_outcome": "Down",
         "realized_simulated_pnl": -.4},
        {"event_type": "shadow_complete", "event_id": "old", "ts": 1,
         "strategy": "late_window_directional_ev", "strategy_config_hash": "old",
         "realized_simulated_pnl": 1},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = build_calibration(path, "new", {"c1": "Down", "c2": "Down"})
    assert report["sample_count"] == 2
    assert report["excluded_other_config"] == 1
    assert report["independent_close_windows"] == 1
    assert report["correlated_close_outcome_groups"] == 1
    assert report["direction_mapping_errors"] == 0
    assert report["official_resolution_verified"] == 2
    assert report["official_resolution_mismatches"] == 0
    assert report["trades"][0]["entry_event_id"] == "entry-a"
    assert report["trades"][0]["volatility_per_sqrt_second"] == .001
    assert report["trades"][0]["model_sample_span_seconds"] == 120
    assert report["trades"][0]["minimum_model_sample_span_seconds"] == 60
    assert report["by_strategy"]["late_window_directional_ev"]["brier_score"] == .64
    assert report["by_strategy"]["late_window_directional_ev"]["maximum_drawdown"] == 6
    assert report["by_strategy"]["late_window_directional_ev"]["maximum_losing_streak"] == 1
    assert report["by_strategy"]["late_window_directional_ev"]["calibration_buckets"]["0.8-0.9"]["realized_hit_rate"] == 0


def test_incomplete_legacy_trade_is_excluded_from_model_metrics(tmp_path):
    path = tmp_path / "execution.jsonl"
    path.write_text(json.dumps({
        "event_type": "shadow_complete", "event_id": "legacy", "ts": 1,
        "strategy": "late_window_directional_ev", "strategy_config_hash": "new",
        "realized_simulated_pnl": -1,
    }), encoding="utf-8")
    report = build_calibration(path, "new")
    assert report["sample_count"] == 1
    assert report["complete_model_samples"] == 0
    assert report["incomplete_model_samples"] == 1
    assert report["by_strategy"]["late_window_directional_ev"]["brier_score"] is None


def test_missing_config_hash_is_json_serializable(tmp_path):
    path = tmp_path / "execution.jsonl"
    path.write_text(json.dumps({
        "event_type": "shadow_complete", "event_id": "legacy", "ts": 1,
        "strategy": "late_window_directional_ev",
    }), encoding="utf-8")
    report = build_calibration(path, "latest")
    assert report["config_hash_counts"] == {"<missing>": 1}
    json.dumps(report, sort_keys=True)


def test_latest_calibration_selects_each_strategy_independently(tmp_path):
    path = tmp_path / "execution.jsonl"
    rows = [
        {"event_type": "shadow_complete", "event_id": "d", "ts": 10,
         "strategy": "late_window_directional_ev", "strategy_config_hash": "directional-v6"},
        {"event_type": "shadow_complete", "event_id": "l", "ts": 11,
         "strategy": "low_price_lottery_ev", "strategy_config_hash": "lottery-v6"},
        {"event_type": "shadow_complete", "event_id": "old", "ts": 1,
         "strategy": "late_window_directional_ev", "strategy_config_hash": "directional-v5"},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    report = build_calibration(path, "latest")

    assert report["config_hash"] == "latest_by_strategy"
    assert report["config_hashes"] == {
        "late_window_directional_ev": "directional-v6",
        "low_price_lottery_ev": "lottery-v6",
    }
    assert report["sample_count"] == 2
    assert report["excluded_other_config"] == 1


def test_official_winner_requires_closed_binary_resolution():
    rows = [
        {"conditionId": "c1", "closed": True,
         "outcomes": '["Up","Down"]', "outcomePrices": '["0","1"]'},
        {"conditionId": "c2", "closed": False,
         "outcomes": '["Up","Down"]', "outcomePrices": '["1","0"]'},
    ]
    assert official_winners(rows) == {"c1": "Down"}


def test_calibration_reads_rotated_and_compressed_history_without_duplicates(tmp_path):
    path = tmp_path / "execution.jsonl"
    row = {"event_type": "shadow_complete", "event_id": "one", "ts": 1,
           "strategy": "late_window_directional_ev", "strategy_config_hash": "new"}
    path.write_text(json.dumps(row), encoding="utf-8")
    (tmp_path / "execution.jsonl.1").write_text(json.dumps(row), encoding="utf-8")
    with gzip.open(tmp_path / "execution.jsonl.2.gz", "wt", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(row, event_id="older", ts=0)))
    report = build_calibration(path, "new")
    assert report["sample_count"] == 2
    assert report["duplicate_completed_events"] == 1
