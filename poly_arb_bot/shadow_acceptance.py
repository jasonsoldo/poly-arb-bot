import json
import os
import time
from pathlib import Path

from .web_monitor import build_status


def evaluate_status(status, max_reference_ipc_age_p95_ms=None,
                    max_clob_to_strategy_p95_us=None):
    readiness = status.get("clob_readiness", {})
    shadow = status.get("shadow_report", {})
    reasons = shadow.get("rejection_reasons", {})
    strategy_counts = status.get("strategy_counts", {})
    strategy_names = ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock")
    complete_set_strategy_names = (
        "split_sell_lock", "inventory_rebalancing_arb", "maker_complete_set_arb",
    )
    strategy_rows = [strategy_counts.get(name, {}) for name in strategy_names]
    probability_rows = [strategy_counts.get(name, {}) for name in strategy_names[:2]]
    counts = status.get("counts", {})
    execution = status.get("shadow_execution", {})
    lifecycle = status.get("shadow_lifecycle", {})
    health = status.get("shadow_health", {})
    market_data_present = readiness.get("discovered_markets", 0) > 0
    max_reference_age = float(
        max_reference_ipc_age_p95_ms if max_reference_ipc_age_p95_ms is not None
        else os.getenv("MAX_REFERENCE_IPC_AGE_P95_MS", "50")
    )
    max_strategy_latency = float(
        max_clob_to_strategy_p95_us if max_clob_to_strategy_p95_us is not None
        else os.getenv("MAX_CLOB_TO_STRATEGY_P95_US", "5000")
    )
    reference_age_p95 = health.get("reference_ipc_receive_age_ms_p95")
    strategy_latency_p95 = health.get("clob_to_strategy_evaluation_us_p95")
    latency_observed = (
        int(health.get("reference_ipc_receive_age_samples", 0)) > 0
        and int(health.get("clob_to_strategy_evaluation_samples", 0)) > 0
        and reference_age_p95 is not None
        and strategy_latency_p95 is not None
    )
    latency_within_budget = (
        not latency_observed
        or (
            float(reference_age_p95) <= max_reference_age
            and float(strategy_latency_p95) <= max_strategy_latency
        )
    )
    real_counters_zero = all(
        field in section and type(section[field]) in (int, float) and section[field] == 0
        for section in (execution, lifecycle)
        for field in ("real_order_submissions", "real_orders", "real_fills")
    )
    checks = [
        {"name": "analytics_ready", "passed": not status.get("analytics_refreshing", False)},
        {"name": "market_data_present", "passed": market_data_present},
        {"name": "clob_websocket_connected",
         "passed": not market_data_present or health.get("ws_connected") is True},
        {"name": "reference_ipc_connected",
         "passed": not market_data_present or health.get("reference_connected") is True},
        {"name": "market_health_fresh",
         "passed": not market_data_present or health.get("stale") is False},
        {"name": "reference_protocol_integrity",
         "passed": not market_data_present or health.get("reference_protocol_errors") == 0},
        {"name": "strategy_audit_no_backpressure",
         "passed": not market_data_present or health.get("strategy_audit_backpressure") == 0},
        {"name": "low_latency_observed",
         "passed": not market_data_present or latency_observed},
        {"name": "low_latency_budget", "passed": latency_within_budget},
        {"name": "audit_data_present", "passed": shadow.get("evaluations", 0) > 0},
        {"name": "market_readiness",
         "passed": readiness.get("paired_markets_ready", 0) + readiness.get("not_ready", 0) == readiness.get("discovered_markets", 0)},
        {"name": "evaluation_decisions",
         "passed": shadow.get("accepted_evaluations", 0) + shadow.get("rejected_evaluations", 0) == shadow.get("evaluations", 0)},
        {"name": "evaluation_reasons",
         "passed": sum(reasons.values()) == shadow.get("rejected_evaluations", 0)},
        {"name": "real_execution_disabled",
         "passed": "executed_orders" in counts and
                   type(counts["executed_orders"]) in (int, float) and
                   counts["executed_orders"] == 0 and
                   real_counters_zero},
        {"name": "event_deduplication", "passed": shadow.get("duplicate_events", 0) == 0},
        {"name": "three_strategy_evaluations",
         "passed": all(row.get("evaluations", 0) > 0 for row in strategy_rows)},
        {"name": "three_strategy_decisions",
         "passed": all(row.get("accepts", 0) + row.get("rejections", 0) == row.get("evaluations", 0)
                       for row in strategy_rows)},
        {"name": "probability_models_evaluated",
         "passed": all(row.get("model_evaluations", 0) > 0 for row in probability_rows)},
        {"name": "terminal_hedge_evaluated",
         "passed": strategy_counts.get("late_window_directional_ev", {}).get(
             "terminal_hedge_evaluations", 0
         ) > 0},
        {"name": "complete_set_strategies_evaluated",
         "passed": all(
             strategy_counts.get(name, {}).get("evaluations", 0) > 0
             for name in complete_set_strategy_names
         )},
    ]
    passed = all(item["passed"] for item in checks)
    incomplete_checks = {"analytics_ready", "market_data_present", "audit_data_present",
                         "three_strategy_evaluations", "probability_models_evaluated",
                         "low_latency_observed", "terminal_hedge_evaluated"}
    incomplete_checks.add("complete_set_strategies_evaluated")
    incomplete_only = all(item["passed"] or item["name"] in incomplete_checks for item in checks)
    status = "PASS" if passed else "INCOMPLETE" if incomplete_only else "FAIL"
    return {"passed": passed, "status": status, "checks": checks,
            "metrics": {"discovered": readiness.get("discovered_markets", 0),
                        "ready": readiness.get("paired_markets_ready", 0),
                        "evaluations": shadow.get("evaluations", 0),
                        "strategy_evaluations": {name: strategy_counts.get(name, {}).get("evaluations", 0)
                                                 for name in strategy_names + complete_set_strategy_names},
                        "terminal_hedge_evaluations": strategy_counts.get(
                            "late_window_directional_ev", {}
                        ).get("terminal_hedge_evaluations", 0),
                        "terminal_hedge_accepts": strategy_counts.get(
                            "late_window_directional_ev", {}
                        ).get("terminal_hedge_accepts", 0),
                        "duplicates": shadow.get("duplicate_events", 0),
                        "reference_ipc_receive_age_ms_p95": reference_age_p95,
                        "clob_to_strategy_evaluation_us_p95": strategy_latency_p95,
                        "max_reference_ipc_age_p95_ms": max_reference_age,
                        "max_clob_to_strategy_p95_us": max_strategy_latency}}


def run(data_dir=Path("data"), log_file=Path("logs/shadow-audit.jsonl"), state_file=Path("state/orders.json"),
        analytics_timeout_seconds=None, analytics_poll_seconds=None):
    timeout = float(analytics_timeout_seconds if analytics_timeout_seconds is not None else
                    os.getenv("SHADOW_ACCEPTANCE_ANALYTICS_TIMEOUT_SECONDS", "120"))
    poll = float(analytics_poll_seconds if analytics_poll_seconds is not None else
                 os.getenv("SHADOW_ACCEPTANCE_ANALYTICS_POLL_SECONDS", "0.25"))
    deadline = time.monotonic() + max(timeout, 0)
    status = build_status(data_dir, log_file, state_file)
    while status.get("analytics_refreshing", False) and time.monotonic() < deadline:
        time.sleep(max(poll, 0))
        status = build_status(data_dir, log_file, state_file)
    report = evaluate_status(status)
    print(json.dumps(report, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(run())
