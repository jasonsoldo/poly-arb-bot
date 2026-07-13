import json
from pathlib import Path

from .web_monitor import build_status


def evaluate_status(status):
    readiness = status.get("clob_readiness", {})
    shadow = status.get("shadow_report", {})
    reasons = shadow.get("rejection_reasons", {})
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
                   status.get("shadow_execution", {}).get("real_order_submissions", 0) == 0},
        {"name": "event_deduplication", "passed": shadow.get("duplicate_events", 0) == 0},
    ]
    return {"passed": all(item["passed"] for item in checks), "checks": checks,
            "metrics": {"discovered": readiness.get("discovered_markets", 0),
                        "ready": readiness.get("paired_markets_ready", 0),
                        "evaluations": shadow.get("evaluations", 0),
                        "duplicates": shadow.get("duplicate_events", 0)}}


def run(data_dir=Path("data"), log_file=Path("logs/shadow-audit.jsonl"), state_file=Path("state/orders.json")):
    report = evaluate_status(build_status(data_dir, log_file, state_file))
    print(json.dumps(report, sort_keys=True))
    return 0 if report["passed"] else 3
