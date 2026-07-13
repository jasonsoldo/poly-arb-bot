import json
import time

from poly_arb_bot.shadow_report import build_report


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


def test_shadow_report_builds_real_simulation_metrics_from_complete_events(tmp_path):
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

    assert report["performance"]["completed"] == 2
    assert report["performance"]["simulated_pnl"] == 0.3
    assert report["performance"]["win_rate"] == 0.5
    assert report["performance"]["sharpe"] is None
    assert report["equity_curve"] == [
        {"ts": 101.0, "pnl": 0.4, "equity": 0.4, "event_id": "m1:100.0"},
        {"ts": 201.0, "pnl": -0.1, "equity": 0.3, "event_id": "m2:200.0"},
    ]
    assert len(report["trade_ledger"]) == 2


def test_shadow_report_keeps_empty_performance_empty(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")

    report = build_report(path, tmp_path / "missing-execution.jsonl")

    assert report["performance"] == {
        "completed": 0, "wins": 0, "losses": 0, "simulated_pnl": 0.0,
        "win_rate": None, "sharpe": None, "sharpe_samples": 0,
    }
    assert report["equity_curve"] == []


def test_shadow_report_quarantines_future_clock_records(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text(json.dumps({
        "ts": time.time() + 3600, "event_type": "shadow_eval", "market_id": "future",
        "reason": "books_not_synced", "fok": True,
    }), encoding="utf-8")

    report = build_report(path)

    assert report["evaluations"] == 0
    assert report["future_events"] == 1
