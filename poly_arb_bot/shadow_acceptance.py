import json
import os
import time
from pathlib import Path

from .ev_shadow import directional_ev_enabled, lottery_ev_enabled
from .web_monitor import build_status


def evaluate_status(status, max_reference_ipc_age_p95_ms=None,
                    max_clob_to_strategy_p95_us=None):
    readiness = status.get("clob_readiness", {})
    shadow = status.get("shadow_report", {})
    reasons = shadow.get("rejection_reasons", {})
    strategy_counts = status.get("strategy_counts", {})
    probability_strategy_names = tuple(
        name for name, enabled in (
            ("late_window_directional_ev", directional_ev_enabled()),
            ("low_price_lottery_ev", lottery_ev_enabled()),
        ) if enabled
    )
    disabled_strategy_names = tuple(
        name for name in ("late_window_directional_ev", "low_price_lottery_ev")
        if name not in probability_strategy_names
    ) + ("split_sell_lock", "maker_complete_set_arb", "microstructure_reversion")
    strategy_names = probability_strategy_names + ("paired_lock", "maker_paired_accumulate")
    strategy_rows = [strategy_counts.get(name, {}) for name in strategy_names]
    probability_rows = [strategy_counts.get(name, {}) for name in probability_strategy_names]
    counts = status.get("counts", {})
    execution = status.get("shadow_execution", {})
    lifecycle = status.get("shadow_lifecycle", {})
    health = status.get("shadow_health", {})
    probability_observations = status.get("probability_observations", {})
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
    engine_started_at = float(health.get("engine_started_at", 0) or 0)
    engine_age_seconds = max(0.0, time.time() - engine_started_at) if engine_started_at else 0.0
    stability_window_seconds = float(os.getenv("WS_STABILITY_MIN_OBSERVATION_SECONDS", "300"))
    stability_observed = engine_age_seconds >= stability_window_seconds
    ws_reconnects = max(0, int(health.get("ws_session_id", 0) or 0) - 1)
    full_resyncs = max(0, int(health.get("full_resyncs", 0) or 0))
    hours_observed = engine_age_seconds / 3600 if stability_observed else 0
    ws_reconnects_per_hour = ws_reconnects / hours_observed if hours_observed else None
    book_resyncs_per_hour = full_resyncs / hours_observed if hours_observed else None
    max_ws_reconnects_per_hour = float(os.getenv("MAX_WS_RECONNECTS_PER_HOUR", "12"))
    max_book_resyncs_per_hour = float(os.getenv("MAX_BOOK_RESYNCS_PER_HOUR", "60"))
    websocket_stability_within_budget = not stability_observed or (
        ws_reconnects_per_hour <= max_ws_reconnects_per_hour
        and book_resyncs_per_hour <= max_book_resyncs_per_hour
    )
    real_counters_zero = all(
        field in section and type(section[field]) in (int, float) and section[field] == 0
        for section in (execution, lifecycle)
        for field in ("real_order_submissions", "real_orders", "real_fills")
    )
    def dynamic_latest_issues(row):
        issues = []
        if row.get("sizing_mode") != "real_market_dynamic_v1":
            issues.append("sizing_mode")
        try:
            requested = float(row.get("requested_max_size"))
            minimum = float(row.get("market_minimum_size"))
            capital = float(row.get("shadow_capital_usd"))
        except (TypeError, ValueError):
            return issues + ["requested_maximum_or_capital"]
        if requested <= 0 or minimum <= 0 or capital <= 0:
            issues.append("requested_maximum_or_capital")
        if row.get("decision") != "ACCEPT":
            return issues
        try:
            target = float(row.get("dynamic_target_size"))
            cost = float(row.get("dynamic_all_in_cost"))
            maximum_loss = float(row.get("dynamic_maximum_loss"))
        except (TypeError, ValueError):
            return issues + ["accepted_position_evidence"]
        if target + 1e-9 < minimum:
            issues.append("target_size_below_market_minimum")
        if cost <= 0:
            issues.append("dynamic_all_in_cost")
        if maximum_loss <= 0:
            issues.append("dynamic_maximum_loss")
        if not row.get("size_binding_constraint"):
            issues.append("size_binding_constraint")
        return issues

    # Dynamic sizing evidence lives on C++ shadow_eval rows (paired_lock and
    # any enabled probability strategies). maker_paired_accumulate episodes
    # carry their own maker-side accounting instead.
    dynamic_sizing_strategy_names = probability_strategy_names + ("paired_lock",)
    dynamic_latest_failures = {
        name: issues
        for name in dynamic_sizing_strategy_names
        if (issues := dynamic_latest_issues(
            status.get("strategy_latest", {}).get(name, {})
        ))
    }
    dynamic_sizing = status.get("dynamic_sizing", {})
    dynamic_sizing_integrity = (
        not dynamic_latest_failures
        and dynamic_sizing.get("invalid_active_positions") == 0
        and dynamic_sizing.get("semantics") == "REAL_MARKET_BOOK_SIZED_SHADOW_NOT_ORDERS"
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
        {"name": "websocket_stability_budget",
         "passed": not market_data_present or websocket_stability_within_budget},
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
        {"name": "probability_observation_integrity", "passed":
         probability_observations.get("semantics") == "CALIBRATION_ONLY_NOT_ORDERS_OR_PNL"},
        {"name": "dynamic_sizing_integrity", "passed": dynamic_sizing_integrity},
        {"name": "event_deduplication", "passed": shadow.get("duplicate_events", 0) == 0},
        {"name": "enabled_strategy_evaluations",
         "passed": all(row.get("evaluations", 0) > 0 for row in strategy_rows)},
        {"name": "enabled_strategy_decisions",
         "passed": all(row.get("accepts", 0) + row.get("rejections", 0) == row.get("evaluations", 0)
                       for row in strategy_rows)},
        {"name": "probability_models_evaluated",
         "passed": not probability_rows or
                   all(row.get("model_evaluations", 0) > 0 for row in probability_rows)},
        {"name": "disabled_strategies_silent",
         "passed": all(
             strategy_counts.get(name, {}).get("evaluations", 0) == 0
             for name in disabled_strategy_names
         )},
    ]
    passed = all(item["passed"] for item in checks)
    incomplete_checks = {"analytics_ready", "market_data_present", "audit_data_present",
                         "enabled_strategy_evaluations", "probability_models_evaluated",
                         "low_latency_observed"}
    incomplete_only = all(item["passed"] or item["name"] in incomplete_checks for item in checks)
    status = "PASS" if passed else "INCOMPLETE" if incomplete_only else "FAIL"
    return {"passed": passed, "status": status, "checks": checks,
            "metrics": {"discovered": readiness.get("discovered_markets", 0),
                        "ready": readiness.get("paired_markets_ready", 0),
                        "evaluations": shadow.get("evaluations", 0),
                        "strategy_evaluations": {name: strategy_counts.get(name, {}).get("evaluations", 0)
                                                 for name in strategy_names + disabled_strategy_names},
                        "duplicates": shadow.get("duplicate_events", 0),
                        "reference_ipc_receive_age_ms_p95": reference_age_p95,
                        "clob_to_strategy_evaluation_us_p95": strategy_latency_p95,
                        "max_reference_ipc_age_p95_ms": max_reference_age,
                        "max_clob_to_strategy_p95_us": max_strategy_latency,
                        "engine_age_seconds": engine_age_seconds,
                        "ws_reconnects": ws_reconnects,
                        "full_resyncs": full_resyncs,
                        "ws_reconnects_per_hour": ws_reconnects_per_hour,
                        "book_resyncs_per_hour": book_resyncs_per_hour,
                        "max_ws_reconnects_per_hour": max_ws_reconnects_per_hour,
                        "max_book_resyncs_per_hour": max_book_resyncs_per_hour,
                        "dynamic_active_positions": dynamic_sizing.get("active_positions"),
                        "dynamic_active_capital_usd": dynamic_sizing.get("active_capital_usd"),
                        "dynamic_maximum_loss_usd": dynamic_sizing.get("maximum_loss_usd"),
                        "dynamic_invalid_active_positions": dynamic_sizing.get(
                            "invalid_active_positions"
                        ),
                        "dynamic_invalid_active_position_reasons": dynamic_sizing.get(
                            "invalid_active_position_reasons", {}
                        ),
                        "dynamic_invalid_active_position_details": dynamic_sizing.get(
                            "invalid_active_position_details", []
                        ),
                        "dynamic_latest_failures": dynamic_latest_failures,
                        "market_health_age_seconds": health.get("age_seconds")}}


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
