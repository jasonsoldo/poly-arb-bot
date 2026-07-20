import json
import sys
import time

from poly_arb_bot import cli
from poly_arb_bot.paired_opportunity_report import (
    PairedOpportunityAccumulator,
    PairedReportConfig,
    build_report,
    format_text,
    run_report,
)


_UNSET = object()


def paired_row(
    ts,
    market_id="m1",
    cost_per_share=1.05,
    shares=5.0,
    up_vwap=0.51,
    down_vwap=0.51,
    reason="net_cost_above_threshold",
    decision="REJECT",
    event_id=None,
    seconds_to_close=600.0,
    asset="BTC",
    timeframe="5m",
    window="current",
    up_fill=5.0,
    down_fill=5.0,
    gross_cost=_UNSET,
    net_cost=_UNSET,
):
    vwap_sum = up_vwap + down_vwap
    if gross_cost is _UNSET:
        gross_cost = shares * vwap_sum
    if net_cost is _UNSET:
        net_cost = shares * cost_per_share
    return {
        "ts": ts,
        "event_id": event_id or f"{market_id}:{ts}",
        "event_type": "shadow_eval",
        "strategy": "paired_lock",
        "market_id": market_id,
        "condition_id": market_id,
        "asset": asset,
        "timeframe": timeframe,
        "window": window,
        "seconds_to_close": seconds_to_close,
        "up_vwap": up_vwap,
        "down_vwap": down_vwap,
        "up_fill": up_fill,
        "down_fill": down_fill,
        "gross_cost": gross_cost,
        "net_cost": net_cost,
        "reason": reason,
        "decision": decision,
        "real_order_submissions": 0,
        "real_orders": 0,
    }


def write_jsonl(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def test_cost_distribution_and_threshold_buckets(tmp_path):
    path = tmp_path / "audit.jsonl"
    costs = [0.99, 0.999, 1.003, 1.008, 1.015, 1.03, 1.05]
    rows = [paired_row(100.0 + index, cost_per_share=cost) for index, cost in enumerate(costs)]
    write_jsonl(path, rows)

    report = build_report(path)

    base = report["evaluation_base"]
    assert base["paired_lock_shadow_eval_events"] == 7
    assert base["valid_evaluations"] == 7
    assert base["excluded"]["total"] == 0

    distribution = report["net_cost_per_share"]
    assert distribution["samples"] == 7
    assert distribution["min"] == 0.99
    assert distribution["median"] == 1.008
    assert distribution["p95"] == 1.05

    buckets = {bucket["below"]: bucket["count"] for bucket in report["threshold_buckets"]}
    assert buckets[0.995] == 1
    assert buckets[1.0] == 2
    assert buckets[1.005] == 3
    assert buckets[1.01] == 4
    assert buckets[1.02] == 5
    assert report["at_or_above_max_threshold"]["count"] == 2

    assert report["opportunities"]["count"] == 2
    assert report["opportunities"]["near_miss_1_0_to_1_01"] == 2


def test_exclusions_and_bad_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        paired_row(100.0, cost_per_share=1.05),
        paired_row(101.0, reason="closing_window", seconds_to_close=12.0, down_fill=0.0),
        paired_row(102.0, seconds_to_close=15.0),  # below min_seconds_to_close
        paired_row(103.0, up_fill=0.0),  # empty book leg
        paired_row(104.0, gross_cost=None),  # incomplete data
        {"event_type": "shadow_eval", "strategy": "late_window_directional_ev", "ts": 105.0},
        {"event_type": "arb_research_summary", "strategy": "paired_lock", "ts": 105.0},
    ]
    payload = "".join(json.dumps(row) + "\n" for row in rows)
    payload += "{broken json line\n"
    payload += "\n"
    path.write_text(payload, encoding="utf-8")

    report = build_report(path)

    base = report["evaluation_base"]
    assert base["paired_lock_shadow_eval_events"] == 5
    assert base["valid_evaluations"] == 1
    assert base["excluded"]["closing_window"] == 2
    assert base["excluded"]["empty_book"] == 1
    assert base["excluded"]["incomplete_data"] == 1
    assert base["invalid_json_lines"] == 1


def test_duplicate_events_counted_separately(tmp_path):
    path = tmp_path / "audit.jsonl"
    row = paired_row(100.0, event_id="dup-1")
    write_jsonl(path, [row, dict(row)])

    report = build_report(path)

    assert report["evaluation_base"]["paired_lock_shadow_eval_events"] == 1
    assert report["evaluation_base"]["duplicate_events"] == 1
    assert report["net_cost_per_share"]["samples"] == 1


def test_opportunity_runs_and_durations(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        paired_row(100.0, cost_per_share=0.99),  # run A start
        paired_row(101.0, cost_per_share=0.995),
        paired_row(102.0, cost_per_share=0.998),  # run A end (3 events, 2s)
        paired_row(200.0, market_id="m2", cost_per_share=0.997),  # other market run
        paired_row(500.0, cost_per_share=0.999),  # gap > 30s: new run B on m1
        paired_row(600.0, cost_per_share=1.05),  # non-qualifying, closes everything
    ]
    write_jsonl(path, rows)

    report = build_report(path)
    runs = report["opportunity_runs"]

    assert runs["open"] == 0
    assert runs["completed"] == 3
    durations = sorted(
        (run["market_id"], run["events"], run["duration_seconds"]) for run in runs["runs"]
    )
    assert ("m1", 3, 2.0) in durations
    assert ("m1", 1, 0.0) in durations
    assert ("m2", 1, 0.0) in durations
    assert runs["duration_seconds"]["max"] == 2.0


def test_groups_breakdown(tmp_path):
    path = tmp_path / "audit.jsonl"
    rows = [
        paired_row(100.0, asset="BTC", timeframe="5m", window="current", cost_per_share=0.99),
        paired_row(101.0, asset="BTC", timeframe="5m", window="current", cost_per_share=1.005),
        paired_row(102.0, asset="ETH", timeframe="15m", window="next", cost_per_share=1.04),
    ]
    write_jsonl(path, rows)

    report = build_report(path)
    groups = {(g["asset"], g["timeframe"], g["window"]): g for g in report["groups"]}

    btc = groups[("BTC", "5m", "current")]
    assert btc["valid"] == 2
    assert btc["opportunities"] == 1
    assert btc["near_miss"] == 1
    assert btc["min_cost"] == 0.99
    eth = groups[("ETH", "15m", "next")]
    assert eth["valid"] == 1
    assert eth["opportunities"] == 0


def test_incremental_watch_resume_matches_full_read(tmp_path):
    path = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    first = [paired_row(100.0 + index, cost_per_share=cost)
             for index, cost in enumerate([0.99, 1.005, 1.05])]
    second = [paired_row(200.0 + index, cost_per_share=cost)
              for index, cost in enumerate([0.998, 1.02])]
    write_jsonl(path, first)

    accumulator = PairedOpportunityAccumulator.load(state)
    accumulator.consume_file(path)
    accumulator.save(state)
    partial = accumulator.report()
    assert partial["evaluation_base"]["valid_evaluations"] == 3

    with path.open("a", encoding="utf-8") as handle:
        for row in second:
            handle.write(json.dumps(row) + "\n")

    resumed = PairedOpportunityAccumulator.load(state)
    resumed.consume_file(path)
    resumed_report = resumed.report()
    full_report = build_report(path)

    assert resumed_report["evaluation_base"] == full_report["evaluation_base"]
    assert resumed_report["net_cost_per_share"] == full_report["net_cost_per_share"]
    assert resumed_report["threshold_buckets"] == full_report["threshold_buckets"]
    assert resumed.state["file"]["offset"] == path.stat().st_size


def test_incremental_partial_line_then_completion(tmp_path):
    path = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    line = json.dumps(paired_row(100.0, cost_per_share=0.99))
    path.write_text(line[: len(line) // 2], encoding="utf-8")

    accumulator = PairedOpportunityAccumulator.load(state)
    accumulator.consume_file(path)
    accumulator.save(state)
    assert accumulator.report()["evaluation_base"]["paired_lock_shadow_eval_events"] == 0

    with path.open("a", encoding="utf-8") as handle:
        handle.write(line[len(line) // 2:] + "\n")

    resumed = PairedOpportunityAccumulator.load(state)
    resumed.consume_file(path)
    report = resumed.report()
    assert report["evaluation_base"]["paired_lock_shadow_eval_events"] == 1
    assert report["evaluation_base"]["invalid_json_lines"] == 0
    assert report["opportunities"]["count"] == 1


def test_truncated_file_resets_offset(tmp_path):
    path = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    write_jsonl(path, [paired_row(100.0 + index, cost_per_share=1.05) for index in range(5)])

    accumulator = PairedOpportunityAccumulator.load(state)
    accumulator.consume_file(path)
    accumulator.save(state)
    assert accumulator.report()["evaluation_base"]["valid_evaluations"] == 5
    assert accumulator.state["file"]["offset"] > 0

    # copytruncate-style rotation: file shrinks below the stored offset.
    # The accumulator must reset the offset and keep accumulating (the five
    # pre-rotation rows were already counted exactly once).
    write_jsonl(path, [paired_row(200.0, cost_per_share=1.01)])
    resumed = PairedOpportunityAccumulator.load(state)
    resumed.consume_file(path)
    report = resumed.report()
    assert report["evaluation_base"]["valid_evaluations"] == 6
    assert report["sample_window"]["first_event_ts"] == 100.0
    assert report["sample_window"]["last_event_ts"] == 200.0
    assert resumed.state["file"]["offset"] == path.stat().st_size


def test_run_report_writes_json_and_text(tmp_path, capsys):
    audit = tmp_path / "audit.jsonl"
    output = tmp_path / "report.json"
    write_jsonl(audit, [paired_row(100.0, cost_per_share=1.05)])

    exit_code = run_report(audit, config=PairedReportConfig(), json_output=output)

    assert exit_code == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["evaluation_base"]["valid_evaluations"] == 1
    text = capsys.readouterr().out
    assert "PAIRED_LOCK OPPORTUNITY REPORT" in text
    assert "sample_window:" in text
    assert "OPPORTUNITIES (net_cost_per_share < 1.0): 0" in text
    assert "NEAR-MISS" in text
    assert "real_orders=0" in text


def test_format_text_marks_no_fake_opportunities(tmp_path):
    path = tmp_path / "audit.jsonl"
    write_jsonl(path, [paired_row(100.0, cost_per_share=1.008)])

    text = format_text(build_report(path))

    assert "OPPORTUNITIES (net_cost_per_share < 1.0): 0" in text
    assert "NEAR-MISS (1.0 <= cost < 1.01, NOT opportunities): 1" in text


def test_cli_paired_opportunity_report(tmp_path, monkeypatch, capsys):
    audit = tmp_path / "audit.jsonl"
    state = tmp_path / "state.json"
    output = tmp_path / "report.json"
    write_jsonl(audit, [paired_row(time.time() - 60, cost_per_share=0.999)])
    monkeypatch.setattr(sys, "argv", [
        "cli", "paired-opportunity-report",
        "--audit-file", str(audit),
        "--watch",
        "--report-state", str(state),
        "--output", str(output),
    ])

    assert cli.main() == 0

    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["opportunities"]["count"] == 1
    assert report["watch"]["offset"] > 0
    assert json.loads(state.read_text(encoding="utf-8"))["valid"] == 1
    assert "OPPORTUNITY RUNS" in capsys.readouterr().out
