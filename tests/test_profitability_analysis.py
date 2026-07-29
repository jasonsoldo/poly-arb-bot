import json
import gzip
import os
from pathlib import Path
import subprocess
import sys

import pytest

from poly_arb_bot.profitability_analysis import (
    aggregate_metrics,
    block_bootstrap_lower_bound,
    build_profitability_report,
    cohort_key,
    fill_price_bucket,
    probability_bucket,
    reconcile_probability_trades,
    seconds_to_close_bucket,
)


STRATEGY = "late_window_directional_ev"
MODEL = "directional_logistic_projected_v2"
CONFIG = "directional-config"


def _write_jsonl(path, rows):
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _entry(event_id, market_id, ts, outcome="Up"):
    return {
        "event_id": event_id,
        "event_type": "shadow_eval",
        "strategy": STRATEGY,
        "market_id": market_id,
        "condition_id": "condition-" + market_id,
        "asset": "BTC",
        "timeframe": "5m",
        "outcome": outcome,
        "decision": "ACCEPT",
        "config_version": "shadow-buy-rules-v9",
        "config_hash": CONFIG,
        "probability_model_id": MODEL,
        "calibration_input_probability": 0.7001,
        "expected_fill_price": 0.4,
        "price_to_beat": 100.0,
        "seconds_to_close": 75,
        "ts": ts,
        "target_depth_ok": True,
        "sizing_mode": "real_market_dynamic_v1",
        "target_size": 10,
        "dynamic_target_size": 10,
        "executable_depth_size": 12,
        "dynamic_vwap": 0.4,
        "dynamic_buy_notional": 4.0,
        "fee_rate": 0.07,
        "dynamic_fee": 0.1,
        "dynamic_buffer": 0.2,
        "dynamic_all_in_cost": 4.3,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }


def _settlement(entry, event_id, ts, winning_outcome="Up"):
    won = entry["outcome"] == winning_outcome
    payout = entry["dynamic_target_size"] if won else 0.0
    close_ts = entry["ts"] + entry["seconds_to_close"]
    return {
        "event_id": event_id,
        "entry_event_id": entry["event_id"],
        "event_type": "shadow_complete",
        "strategy": entry["strategy"],
        "strategy_config_hash": entry["config_hash"],
        "probability_model_id": entry["probability_model_id"],
        "market_id": entry["market_id"],
        "condition_id": entry["condition_id"],
        "asset": entry["asset"],
        "timeframe": entry["timeframe"],
        "outcome": entry["outcome"],
        "entry_ts": entry["ts"],
        "close_ts": close_ts,
        "ts": ts,
        "target_size": entry["dynamic_target_size"],
        "winning_outcome": winning_outcome,
        "settlement_price": 101.0 if winning_outcome == "Up" else 99.0,
        "settlement_timestamp_ms": close_ts * 1000,
        "payout": payout,
        "realized_simulated_pnl": payout - 4.1,
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }


def _profit_exit(entry, event_id, ts):
    return {
        **_settlement(entry, event_id, ts),
        "completion_reason": "profit_target_book_executable",
        "exit_fill_quantity": 10,
        "exit_vwap": 0.45,
        "exit_total_fee": 0.02,
        "exit_execution_buffer": 0.01,
        "exit_depth_ok": True,
        "exit_book_fresh": True,
        "exit_observation_semantics": "BOOK_EXECUTABLE_NOT_FILL",
        # Legacy v9 recorded PnL incorrectly included the non-cash buffer.
        "payout": 4.47,
        "realized_simulated_pnl": 0.37,
    }


def test_reconciliation_uses_first_market_entry_and_recomputes_cash_pnl(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    first = _entry("entry-1", "m1", 100)
    later = _entry("entry-2", "m1", 200)
    missing_fee = _entry("entry-3", "m2", 300)
    missing_fee.pop("dynamic_fee")
    wrong_outcome = _entry("entry-4", "m3", 400)
    _write_jsonl(audit, [first, later, missing_fee, wrong_outcome])
    first_complete = _profit_exit(first, "complete-1", 150)
    mismatched = _settlement(wrong_outcome, "complete-4", 450)
    mismatched["outcome"] = "Down"
    _write_jsonl(
        execution,
        [
            first_complete,
            first_complete,
            _settlement(later, "complete-2", 280),
            _settlement(missing_fee, "complete-3", 380),
            mismatched,
        ],
    )

    result = reconcile_probability_trades(audit, execution)

    assert len(result["trades"]) == 1
    assert result["trades"][0]["market_id"] == "m1"
    assert result["trades"][0]["entry_event_id"] == "entry-1"
    assert result["trades"][0]["net_pnl_usd"] == pytest.approx(0.38)
    assert result["trades"][0]["net_return_per_dollar_risked"] == pytest.approx(
        0.38 / 4.1
    )
    assert result["excluded"]["duplicate_event"] == 1
    assert result["excluded"]["duplicate_market"] == 1
    assert result["excluded"]["fee_schedule_unavailable"] == 1
    assert result["excluded"]["outcome_mismatch"] == 1
    assert result["selected_config_hashes"] == {STRATEGY: CONFIG}


def test_reconciliation_fails_closed_on_real_order_evidence(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    complete = _settlement(entry, "complete-1", 180)
    complete["real_orders"] = 1
    _write_jsonl(audit, [entry])
    _write_jsonl(execution, [complete])

    result = reconcile_probability_trades(audit, execution)

    assert result["trades"] == []
    assert result["excluded"]["real_order_invariant"] == 1


def test_v10_cash_maximum_loss_must_match_recomputed_entry_cash(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    entry.update({
        "config_version": "shadow-buy-rules-v10",
        "dynamic_cash_cost": 4.1,
        "dynamic_risk_adjusted_cost": 4.3,
        "dynamic_maximum_loss": 4.3,
    })
    complete = _settlement(entry, "complete-1", 180)
    complete.update({
        "cash_ledger_version": 2,
        "deployable_pnl": True,
    })
    _write_jsonl(audit, [entry])
    _write_jsonl(execution, [complete])

    result = reconcile_probability_trades(audit, execution)

    assert result["trades"] == []
    assert result["excluded"]["pnl_recalculation_mismatch"] == 1


def test_settlement_winner_is_recomputed_from_price_evidence(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    complete = _settlement(entry, "complete-1", 180)
    complete["settlement_price"] = 99.0
    _write_jsonl(audit, [entry])
    _write_jsonl(execution, [complete])

    result = reconcile_probability_trades(audit, execution)

    assert result["trades"] == []
    assert result["excluded"]["pnl_recalculation_mismatch"] == 1


def test_settlement_timestamp_must_be_in_canonical_close_window(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    complete = _settlement(entry, "complete-1", 180)
    complete["settlement_timestamp_ms"] += 10_001
    _write_jsonl(audit, [entry])
    _write_jsonl(execution, [complete])

    result = reconcile_probability_trades(audit, execution)

    assert result["trades"] == []
    assert result["excluded"]["pnl_recalculation_mismatch"] == 1


def test_temporal_evidence_must_match_entry_and_close(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    entry["seconds_to_close"] = 74
    complete = _profit_exit(entry, "complete-1", 180)
    complete["close_ts"] = 175
    _write_jsonl(audit, [entry])
    _write_jsonl(execution, [complete])

    result = reconcile_probability_trades(audit, execution)

    assert result["trades"] == []
    assert result["excluded"]["pnl_recalculation_mismatch"] == 1


def test_positive_fee_rate_requires_positive_dynamic_fee(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    entry["dynamic_fee"] = 0
    entry["dynamic_all_in_cost"] = 4.2
    complete = _profit_exit(entry, "complete-1", 150)
    _write_jsonl(audit, [entry])
    _write_jsonl(execution, [complete])

    result = reconcile_probability_trades(audit, execution)

    assert result["trades"] == []
    assert result["excluded"]["fee_schedule_unavailable"] == 1


def test_bucket_boundaries_and_cohort_key_are_fixed():
    assert probability_bucket(0.1007) == "0.1-0.2"
    assert fill_price_bucket(0.027) == "0.0-0.1"
    assert seconds_to_close_bucket(15) == "0-30"
    assert seconds_to_close_bucket(75) == "60-90"
    assert seconds_to_close_bucket(588) == "300-600"
    assert cohort_key(
        {
            "strategy": STRATEGY,
            "asset": "BTC",
            "timeframe": "5m",
            "outcome": "Up",
            "calibration_input_probability": 0.7001,
            "expected_fill_price": 0.4,
            "seconds_to_close": 75,
        }
    ) == (
        "strategy=late_window_directional_ev|asset=BTC|timeframe=5m|"
        "outcome=Up|probability=0.7-0.8|fill=0.4-0.5|seconds=60-90"
    )


def test_block_bootstrap_is_deterministic_and_resamples_whole_utc_blocks():
    rows = [
        {"market_id": "m1", "close_ts": 0, "net_return_per_dollar_risked": 0.1},
        {"market_id": "m2", "close_ts": 0, "net_return_per_dollar_risked": 0.3},
        {
            "market_id": "m3",
            "close_ts": 14_400,
            "net_return_per_dollar_risked": -0.2,
        },
        {
            "market_id": "m4",
            "close_ts": 14_400,
            "net_return_per_dollar_risked": -0.4,
        },
    ]

    first = block_bootstrap_lower_bound(rows, "same-seed")
    second = block_bootstrap_lower_bound(list(reversed(rows)), "same-seed")

    assert first == second
    assert first == pytest.approx(-0.3)


def test_aggregate_metrics_only_seed_bootstrap_sampling():
    rows = [
        {
            "market_id": "m1",
            "close_ts": index * 14_400,
            "net_pnl_usd": value,
            "entry_cash_usd": 1.0,
            "completion_cash_usd": 1.0 + value,
            "net_return_per_dollar_risked": value,
        }
        for index, value in enumerate((0.8, -0.2, 0.5, -0.4, 0.1, 0.3))
    ]

    first = aggregate_metrics(rows, "seed-a")
    repeated = aggregate_metrics(rows, "seed-a")
    other = aggregate_metrics(rows, "seed-b")

    assert json.dumps(first, sort_keys=True) == json.dumps(repeated, sort_keys=True)
    assert {
        key: value for key, value in first.items() if key != "lower_bound_95"
    } == {
        key: value for key, value in other.items() if key != "lower_bound_95"
    }


def test_report_reads_rotated_history_and_reports_blocking_exclusions(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    entry = _entry("entry-1", "m1", 100)
    _write_jsonl(Path(str(audit) + ".1"), [entry])
    _write_jsonl(audit, [])
    _write_jsonl(Path(str(execution) + ".1"), [_settlement(entry, "complete-1", 180)])
    execution.write_text("{not-json}\n", encoding="utf-8")

    report = build_profitability_report(audit, execution)

    assert report["generated_at"] == 180
    assert report["independent_markets"] == 1
    assert report["blocking_exclusions"] == {"invalid_json": 1}
    assert report["cash_ledger"]["net_pnl_usd"] == pytest.approx(5.9)
    assert report["real_order_submissions"] == 0
    assert report["real_orders"] == 0
    assert report["real_fills"] == 0
    assert len(report["source"]["strategy_audit"]["files"]) == 2
    assert len(report["source"]["execution_log"]["files"]) == 2
    assert json.dumps(report, sort_keys=True) == json.dumps(
        build_profitability_report(audit, execution),
        sort_keys=True,
    )


def test_truncated_rotated_gzip_is_a_blocking_exclusion(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    rotated = Path(str(execution) + ".1.gz")
    _write_jsonl(audit, [])
    _write_jsonl(execution, [])
    with gzip.open(rotated, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"event_type": "shadow_complete"}) + "\n")
    encoded = rotated.read_bytes()
    rotated.write_bytes(encoded[:-8])

    report = build_profitability_report(audit, execution)

    assert report["excluded"]["source_read_error"] == 1
    assert report["blocking_exclusions"]["invalid_json"] == 1


def test_cli_writes_only_the_requested_profitability_report(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    output = tmp_path / "reports" / "profitability.json"
    gate = tmp_path / "profitability-gates.json"
    _write_jsonl(audit, [])
    _write_jsonl(execution, [])
    gate.write_text("sentinel", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "poly_arb_bot.cli",
            "profitability-analysis",
            "--strategy-audit-file",
            str(audit),
            "--execution-log",
            str(execution),
            "--output",
            str(output),
        ],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["independent_markets"] == 0
    assert gate.read_text(encoding="utf-8") == "sentinel"


def test_cli_rejects_output_that_would_overwrite_gate_or_input(tmp_path):
    audit = tmp_path / "strategy-audit.jsonl"
    execution = tmp_path / "shadow-execution.jsonl"
    gate = tmp_path / "profitability-gates.json"
    _write_jsonl(audit, [])
    _write_jsonl(execution, [])
    gate.write_text("sentinel", encoding="utf-8")
    environment = os.environ.copy()
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"

    for protected in (gate, audit):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "poly_arb_bot.cli",
                "profitability-analysis",
                "--strategy-audit-file",
                str(audit),
                "--execution-log",
                str(execution),
                "--output",
                str(protected),
            ],
            cwd=Path(__file__).parents[1],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert completed.returncode == 2

    assert gate.read_text(encoding="utf-8") == "sentinel"
    assert audit.read_text(encoding="utf-8") == ""
