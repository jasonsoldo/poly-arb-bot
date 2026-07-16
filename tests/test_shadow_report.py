import json
import time

from poly_arb_bot.ev_shadow import strategy_config
from poly_arb_bot.shadow_report import IncrementalReport, build_report


def test_shadow_report_aggregates_reasons_and_percentiles(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        {"event_type": "shadow_eval", "market_id": "m1", "reason": "no_edge", "fok": True, "source_age_ms": 10},
        {"event_type": "shadow_eval", "market_id": "m1", "reason": "books_not_synced", "fok": False, "source_age_ms": 30},
        {"event_type": "shadow_opportunity", "market_id": "m1", "duration_ms": 25},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = build_report(path)
    assert report["evaluations"] == 2
    assert report["fok_passed"] == 1
    assert report["accepts"] == 1
    assert report["rejection_reasons"] == {"no_edge": 1, "books_not_synced": 1}
    assert report["source_age_ms"]["p95"] == 30


def test_execution_complete_is_not_counted_as_settled_simulation(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    audit.write_text("\n".join([
        json.dumps({"ts": 100.0, "event_type": "shadow_opportunity", "market_id": "m1", "expected_execution_value": 0.4}),
        json.dumps({"ts": 200.0, "event_type": "shadow_opportunity", "market_id": "m2", "expected_execution_value": -0.1}),
    ]), encoding="utf-8")
    execution.write_text("\n".join([
        json.dumps({"ts": 101.0, "event_type": "shadow_execution", "event_id": "m1:100.0", "market_id": "m1", "state": "COMPLETE"}),
        json.dumps({"ts": 102.0, "event_type": "shadow_execution", "event_id": "m1:100.0", "market_id": "m1", "state": "COMPLETE"}),
        json.dumps({"ts": 201.0, "event_type": "shadow_execution", "event_id": "m2:200.0", "market_id": "m2", "state": "COMPLETE"}),
    ]), encoding="utf-8")

    report = build_report(audit, execution)

    assert report["performance"]["completed"] == 0
    assert report["performance"]["simulated_pnl"] is None
    assert report["equity_curve"] == []


def test_shadow_report_keeps_empty_performance_empty(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")

    report = build_report(path, tmp_path / "missing-execution.jsonl")

    assert report["performance"] == {
        "completed": 0, "wins": 0, "losses": 0, "simulated_pnl": None,
        "win_rate": None, "sharpe": None, "sharpe_samples": 0,
    }
    assert report["equity_curve"] == []


def test_shadow_report_uses_realized_shadow_complete_pnl(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    audit.write_text("", encoding="utf-8")
    current_hash = strategy_config()[1]
    execution.write_text(json.dumps({
        "ts": 1101, "event_type": "shadow_complete", "event_id": "p1",
        "strategy": "late_window_directional_ev", "market_id": "m1",
        "strategy_config_version": "shadow-buy-rules-v2",
        "strategy_config_hash": current_hash,
        "realized_simulated_pnl": 5.9,
    }) + "\n", encoding="utf-8")
    report = build_report(audit, execution)
    assert report["performance"]["completed"] == 1
    assert report["performance"]["simulated_pnl"] == 5.9
    assert report["performance_by_strategy"]["late_window_directional_ev"]["completed"] == 1
    assert report["performance_by_strategy"]["paired_lock"]["completed"] == 0


def test_shadow_report_keeps_terminal_hedge_fields_in_ledger(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    audit.write_text("", encoding="utf-8")
    current_hash = strategy_config("late_window_directional_ev")[1]
    execution.write_text(json.dumps({
        "ts": 1101, "event_type": "shadow_complete", "event_id": "h1",
        "strategy": "late_window_directional_ev", "market_id": "m1",
        "strategy_config_hash": current_hash, "realized_simulated_pnl": -0.4,
        "execution_mode": "terminal_hedged", "main_outcome": "Up",
        "hedge_outcome": "Down", "main_size": 10, "hedge_size": 8.5,
        "total_entry_cost": 8.9,
    }) + "\n", encoding="utf-8")

    row = build_report(audit, execution)["trade_ledger"][0]

    assert row["execution_mode"] == "terminal_hedged"
    assert row["main_outcome"] == "Up"
    assert row["hedge_outcome"] == "Down"
    assert row["hedge_size"] == 8.5


def test_shadow_report_excludes_other_hash_from_current_performance(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    audit.write_text("", encoding="utf-8")
    current_hash = strategy_config()[1]
    rows = [
        {"ts": 1, "event_type": "shadow_complete", "event_id": "current",
         "strategy": "late_window_directional_ev", "strategy_config_hash": current_hash,
         "realized_simulated_pnl": 1},
        {"ts": 2, "event_type": "shadow_complete", "event_id": "old",
         "strategy": "late_window_directional_ev", "strategy_config_hash": "old-hash",
         "realized_simulated_pnl": -10},
    ]
    execution.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    report = build_report(audit, execution)
    assert report["performance"]["completed"] == 1
    assert report["performance"]["simulated_pnl"] == 1
    assert report["excluded_other_strategy_config"] == 1


def test_shadow_report_excludes_unversioned_historical_paired_completions(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    audit.write_text("", encoding="utf-8")
    execution.write_text("\n".join([
        json.dumps({
            "ts": 1, "event_type": "shadow_complete", "event_id": "historical",
            "strategy": "paired_lock", "realized_simulated_pnl": 4.05,
        }),
        json.dumps({
            "ts": 2, "event_type": "shadow_complete", "event_id": "current",
            "strategy": "paired_lock", "strategy_config_hash": "paired-current",
            "realized_simulated_pnl": 0.2,
        }),
    ]), encoding="utf-8")

    report = build_report(audit, execution)

    assert report["performance"]["completed"] == 1
    assert report["performance"]["simulated_pnl"] == 0.2
    assert report["excluded_other_strategy_config"] == 1


def test_shadow_report_quarantines_future_clock_records(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({
        "ts": time.time() + 3600, "event_type": "shadow_eval", "market_id": "future",
        "reason": "books_not_synced", "fok": True,
    }), encoding="utf-8")

    report = build_report(path)

    assert report["evaluations"] == 0
    assert report["future_events"] == 1


def test_shadow_report_deduplicates_stable_evaluation_ids(tmp_path):
    path = tmp_path / "audit.jsonl"
    row = {"ts": time.time(), "event_id": "1:1:m1:7", "event_type": "shadow_eval",
           "market_id": "m1", "decision": "REJECT", "reason": "no_edge", "fok": True}
    path.write_text("\n".join([json.dumps(row), json.dumps(row)]), encoding="utf-8")

    report = build_report(path)

    assert report["evaluations"] == 1
    assert report["duplicate_events"] == 1
    assert report["rejected_evaluations"] == 1
    assert report["accepted_evaluations"] == 0


def test_incremental_report_reads_only_appended_complete_lines(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    state = tmp_path / "summary.json"
    first = {
        "ts": time.time(), "event_id": "e1", "event_type": "shadow_eval",
        "market_id": "m1", "decision": "REJECT", "reason": "no_edge",
    }
    audit.write_text(json.dumps(first) + "\n", encoding="utf-8")
    analytics = IncrementalReport(audit, execution, state)

    assert analytics.refresh()["evaluations"] == 1
    initial_bytes = analytics.last_bytes_read
    initial_size = audit.stat().st_size
    second = dict(first, event_id="e2", market_id="m2")
    encoded = json.dumps(second) + "\n"
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(encoded)
    appended_bytes = audit.stat().st_size - initial_size

    assert analytics.refresh()["evaluations"] == 2
    assert analytics.last_bytes_read == appended_bytes
    assert analytics.last_bytes_read < initial_bytes + appended_bytes


def test_incremental_report_resumes_from_persisted_summary(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    state = tmp_path / "summary.json"
    audit.write_text(json.dumps({
        "ts": time.time(), "event_id": "e1", "event_type": "shadow_eval",
        "market_id": "m1", "decision": "REJECT", "reason": "no_edge",
    }) + "\n", encoding="utf-8")
    assert IncrementalReport(audit, execution, state).refresh()["evaluations"] == 1

    restarted = IncrementalReport(audit, execution, state)
    assert restarted.refresh()["evaluations"] == 1
    assert restarted.last_bytes_read == 0


def test_incremental_report_handles_rotation_and_partial_final_line(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    state = tmp_path / "summary.json"
    row = {
        "ts": time.time(), "event_id": "e1", "event_type": "shadow_eval",
        "market_id": "m1", "decision": "REJECT", "reason": "no_edge",
    }
    audit.write_text(json.dumps(row) + "\n", encoding="utf-8")
    analytics = IncrementalReport(audit, execution, state)
    assert analytics.refresh()["evaluations"] == 1

    audit.replace(tmp_path / "audit.jsonl.1")
    complete = json.dumps(dict(row, event_id="e2"))
    partial = complete[:-2]
    audit.write_text(partial, encoding="utf-8")
    assert analytics.refresh()["evaluations"] == 1
    with audit.open("a", encoding="utf-8") as handle:
        handle.write(complete[-2:] + "\n")
    assert analytics.refresh()["evaluations"] == 2


def test_incremental_report_deduplicates_replayed_event_after_rotation(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    state = tmp_path / "summary.json"
    row = {
        "ts": time.time(), "event_id": "stable", "event_type": "shadow_eval",
        "market_id": "m1", "decision": "REJECT", "reason": "no_edge",
    }
    audit.write_text(json.dumps(row) + "\n", encoding="utf-8")
    analytics = IncrementalReport(audit, execution, state)
    assert analytics.refresh()["evaluations"] == 1
    audit.replace(tmp_path / "audit.jsonl.1")
    audit.write_text(json.dumps(row) + "\n", encoding="utf-8")
    report = analytics.refresh()
    assert report["evaluations"] == 1
    assert report["duplicate_events"] == 1


def test_incremental_report_matches_clean_full_build(tmp_path):
    audit = tmp_path / "audit.jsonl"
    execution = tmp_path / "execution.jsonl"
    state = tmp_path / "summary.json"
    current_hash = strategy_config()[1]
    audit_rows = [
        {"ts": time.time(), "event_id": "e1", "event_type": "shadow_eval",
         "market_id": "m1", "decision": "REJECT", "reason": "no_edge",
         "fok": True, "source_age_ms": 12},
        {"ts": time.time(), "event_id": "e2", "event_type": "shadow_opportunity",
         "market_id": "m1", "duration_ms": 50},
    ]
    execution_rows = [
        {"ts": time.time(), "event_id": "c1", "event_type": "shadow_complete",
         "strategy": "late_window_directional_ev", "strategy_config_hash": current_hash,
         "market_id": "m1", "asset": "BTC", "timeframe": "5m",
         "realized_simulated_pnl": 0.25},
    ]
    audit.write_text("\n".join(map(json.dumps, audit_rows)) + "\n", encoding="utf-8")
    execution.write_text("\n".join(map(json.dumps, execution_rows)) + "\n", encoding="utf-8")

    incremental = IncrementalReport(audit, execution, state).refresh()
    full = build_report(audit, execution)

    assert incremental == full
