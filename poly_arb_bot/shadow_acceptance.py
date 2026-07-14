import json
from pathlib import Path

from .web_monitor import build_status


def evaluate_status(status):
    readiness = status.get("clob_readiness", {})
    shadow = status.get("shadow_report", {})
    reasons = shadow.get("rejection_reasons", {})
    strategy_counts = status.get("strategy_counts", {})
    strategy_names = ("late_window_directional_ev", "low_price_lottery_ev", "paired_lock")
    strategy_rows = [strategy_counts.get(name, {}) for name in strategy_names]
    probability_rows = [strategy_counts.get(name, {}) for name in strategy_names[:2]]
    checks = [
        {"name": "market_data_present", "passed": readiness.get("discovered_markets", 0) > 0},
        {"name": "audit_data_present", "passed": shadow.get("evaluations", 0) > 0},
        {"name": "market_readiness",
         "passed": readiness.get("paired_markets_ready", 0) + readiness.get("not_ready", 0) == readiness.get("discovered_markets", 0)},
        {"name": "evaluation_decisions",
         "passed": shadow.get("accepted_evaluations", 0) + shadow.get("rejected_evaluations", 0) == shadow.get("evaluations", 0)},
        {"name": "evaluation_reasons",
         "passed": sum(reasons.values()) == shadow.get("rejected_evaluations", 0)},
        {"name": "real_execution_disabled",
         "passed": status.get("counts", {}).get("executed_orders", 0) == 0 and
                   status.get("shadow_execution", {}).get("real_order_submissions", 0) == 0 and
                   status.get("shadow_lifecycle", {}).get("real_order_submissions", 0) == 0 and
                   status.get("shadow_lifecycle", {}).get("real_orders", 0) == 0},
        {"name": "event_deduplication", "passed": shadow.get("duplicate_events", 0) == 0},
        {"name": "three_strategy_evaluations",
         "passed": all(row.get("evaluations", 0) > 0 for row in strategy_rows)},
        {"name": "three_strategy_decisions",
         "passed": all(row.get("accepts", 0) + row.get("rejections", 0) == row.get("evaluations", 0)
                       for row in strategy_rows)},
        {"name": "probability_models_evaluated",
         "passed": all(row.get("model_evaluations", 0) > 0 for row in probability_rows)},
    ]
    passed = all(item["passed"] for item in checks)
    incomplete_checks = {"market_data_present", "audit_data_present", "three_strategy_evaluations",
                         "probability_models_evaluated"}
    incomplete_only = all(item["passed"] or item["name"] in incomplete_checks for item in checks)
    status = "PASS" if passed else "INCOMPLETE" if incomplete_only else "FAIL"
    return {"passed": passed, "status": status, "checks": checks,
            "metrics": {"discovered": readiness.get("discovered_markets", 0),
                        "ready": readiness.get("paired_markets_ready", 0),
                        "evaluations": shadow.get("evaluations", 0),
                        "strategy_evaluations": {name: strategy_counts.get(name, {}).get("evaluations", 0)
                                                 for name in strategy_names},
                        "duplicates": shadow.get("duplicate_events", 0)}}


def run(data_dir=Path("data"), log_file=Path("logs/shadow-audit.jsonl"), state_file=Path("state/orders.json")):
    report = evaluate_status(build_status(data_dir, log_file, state_file))
    print(json.dumps(report, sort_keys=True))
    return {"PASS": 0, "FAIL": 1, "INCOMPLETE": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(run())
