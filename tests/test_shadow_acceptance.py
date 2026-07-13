from poly_arb_bot.shadow_acceptance import evaluate_status


def valid_status():
    return {
        "clob_readiness": {"discovered_markets": 4, "paired_markets_ready": 3, "not_ready": 1},
        "shadow_report": {
            "evaluations": 10, "accepted_evaluations": 2, "rejected_evaluations": 8,
            "rejection_reasons": {"no_edge": 6, "depth": 2}, "duplicate_events": 0,
        },
        "counts": {"executed_orders": 0},
        "shadow_execution": {"real_order_submissions": 0},
        "performance": {"completed": 0},
    }


def test_acceptance_passes_all_shadow_invariants():
    report = evaluate_status(valid_status())
    assert report["passed"] is True
    assert all(check["passed"] for check in report["checks"])


def test_acceptance_fails_drift_and_real_order_submission():
    status = valid_status()
    status["clob_readiness"]["not_ready"] = 2
    status["shadow_report"]["rejection_reasons"]["no_edge"] = 5
    status["shadow_execution"]["real_order_submissions"] = 1
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert report["passed"] is False
    assert failed == {"market_readiness", "evaluation_reasons", "real_execution_disabled"}
