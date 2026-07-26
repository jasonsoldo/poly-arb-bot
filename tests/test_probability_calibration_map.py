import json

from poly_arb_bot.probability_calibration_map import (
    bucket_index, bucket_name, build_calibration_map, calibrate_probability,
    load_calibration_map, publish_calibration_map,
)


def _row(strategy="late_window_directional_ev", timeframe="5m",
         probability=0.95, actual_up=1, event_id="e1"):
    return {
        "event_type": "shadow_prediction_complete", "event_id": event_id,
        "strategy": strategy, "timeframe": timeframe,
        "estimated_up_probability": probability, "actual_up": actual_up,
    }


def _write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _payload(bucket_stats, generated_at=1000.0, timeframe="5m"):
    """Minimal map payload: one strategy with one timeframe bucket."""
    buckets = {}
    overall = {}
    for (scope_tf, name), stats in bucket_stats.items():
        target = buckets if scope_tf else overall
        target[name] = stats
    return {
        "version": 1, "generated_at": generated_at,
        "config": {"min_bucket_samples": 30, "prior_weight": 30.0},
        "strategies": {
            "late_window_directional_ev": {
                "timeframes": {timeframe: buckets} if buckets else {},
                "overall": overall,
            },
        },
    }


def test_bucket_index_and_name_match_consumer_convention():
    assert bucket_index(0.0) == 0
    assert bucket_index(0.949) == 9
    assert bucket_index(1.0) == 9
    assert bucket_index(-0.5) == 0
    assert bucket_name(9) == "0.9-1.0"
    assert bucket_name(0) == "0.0-0.1"


def test_build_aggregates_per_strategy_timeframe_and_overall(tmp_path):
    log = tmp_path / "exec.jsonl"
    _write(log, [
        _row(probability=0.95, actual_up=1, event_id="a"),
        _row(probability=0.92, actual_up=0, event_id="b"),
        _row(timeframe="15m", probability=0.91, actual_up=1, event_id="c"),
        _row(strategy="low_price_lottery_ev", probability=0.04, actual_up=0, event_id="d"),
        {"event_type": "shadow_eval", "strategy": "late_window_directional_ev"},  # ignored
    ])
    payload = build_calibration_map(log, now=1000.0)
    directional = payload["strategies"]["late_window_directional_ev"]
    bucket = directional["timeframes"]["5m"]["0.9-1.0"]
    assert bucket["samples"] == 2
    assert bucket["realized_up_rate"] == 0.5
    assert bucket["expected_up_rate"] == round((0.95 + 0.92) / 2, 12)
    assert directional["overall"]["0.9-1.0"]["samples"] == 3
    assert directional["timeframes"]["15m"]["0.9-1.0"]["realized_up_rate"] == 1.0
    lottery = payload["strategies"]["low_price_lottery_ev"]
    assert lottery["overall"]["0.0-0.1"]["realized_up_rate"] == 0.0
    assert payload["generated_at"] == 1000.0
    assert payload["config"]["min_bucket_samples"] == 30
    assert payload["config"]["prior_weight"] == 30.0


def test_build_reads_rotated_history(tmp_path):
    current = tmp_path / "exec.jsonl"
    rotated = tmp_path / "exec.jsonl.1"
    _write(rotated, [_row(event_id="old")])
    _write(current, [_row(event_id="new", actual_up=0)])
    payload = build_calibration_map(current)
    bucket = payload["strategies"]["late_window_directional_ev"]["overall"]["0.9-1.0"]
    assert bucket["samples"] == 2
    assert bucket["realized_up_rate"] == 0.5


def test_publish_is_atomic_and_loadable(tmp_path):
    log = tmp_path / "exec.jsonl"
    _write(log, [_row()])
    output = tmp_path / "data" / "probability-calibration-map.json"
    payload = publish_calibration_map(log, output, now=1000.0)
    assert not (tmp_path / "data" / "probability-calibration-map.json.tmp").exists()
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded == payload


def test_rows_missing_required_fields_are_skipped(tmp_path):
    log = tmp_path / "exec.jsonl"
    _write(log, [
        {**_row(), "estimated_up_probability": None},
        {**_row(), "timeframe": ""},
        {**_row(), "actual_up": None},
        _row(event_id="ok"),
    ])
    payload = build_calibration_map(log)
    assert payload["strategies"]["late_window_directional_ev"]["overall"]["0.9-1.0"]["samples"] == 1


# --- Consumer side (mirrors the C++ calibrate_probability) ---


def test_calibrate_applies_shrinkage_with_timeframe_scope():
    payload = _payload({
        ("5m", "0.8-0.9"): {"samples": 100, "expected_up_rate": 0.85, "realized_up_rate": 0.70},
    })
    result = calibrate_probability(
        payload, "late_window_directional_ev", "5m", 0.86, now=1010.0)
    assert result["scope"] == "timeframe"
    assert result["bucket"] == "0.8-0.9"
    assert result["samples"] == 100
    assert result["input_probability"] == 0.86
    # (70 + 30 * 0.85) / (100 + 30)
    assert abs(result["probability"] - 95.5 / 130) < 1e-12


def test_calibrate_falls_back_to_strategy_overall():
    payload = _payload({
        (None, "0.8-0.9"): {"samples": 40, "expected_up_rate": 0.80, "realized_up_rate": 0.50},
    })
    result = calibrate_probability(
        payload, "late_window_directional_ev", "15m", 0.86, now=1010.0)
    assert result["scope"] == "strategy"
    assert abs(result["probability"] - (20 + 30 * 0.80) / 70) < 1e-12


def test_calibrate_insufficient_samples_fail_closed_by_default():
    payload = _payload({
        ("5m", "0.8-0.9"): {"samples": 5, "expected_up_rate": 0.85, "realized_up_rate": 0.70},
    })
    result = calibrate_probability(
        payload, "late_window_directional_ev", "5m", 0.86, now=1010.0)
    assert result["probability"] is None
    assert result["scope"] == "none"


def test_calibrate_insufficient_samples_raw_fallback_when_not_required():
    payload = _payload({
        ("5m", "0.8-0.9"): {"samples": 5, "expected_up_rate": 0.85, "realized_up_rate": 0.70},
    })
    result = calibrate_probability(
        payload, "late_window_directional_ev", "5m", 0.86, now=1010.0,
        require_map=False)
    assert result["probability"] == 0.86
    assert result["scope"] == "raw"


def test_calibrate_stale_map_fail_closed():
    payload = _payload({
        ("5m", "0.8-0.9"): {"samples": 100, "expected_up_rate": 0.85, "realized_up_rate": 0.70},
    }, generated_at=100.0)
    result = calibrate_probability(
        payload, "late_window_directional_ev", "5m", 0.86, now=1010.0)
    assert result["probability"] is None
    assert result["scope"] == "none"


def test_calibrate_missing_map_fail_closed():
    result = calibrate_probability(None, "late_window_directional_ev", "5m", 0.86, now=1010.0)
    assert result["probability"] is None
    assert result["bucket"] == "0.8-0.9"
    assert result["scope"] == "none"


def test_load_calibration_map_reads_file(tmp_path):
    output = tmp_path / "map.json"
    payload = _payload({
        ("5m", "0.8-0.9"): {"samples": 100, "expected_up_rate": 0.85, "realized_up_rate": 0.70},
    })
    output.write_text(json.dumps(payload), encoding="utf-8")
    assert load_calibration_map(output) == payload
    assert load_calibration_map(tmp_path / "missing.json") is None
