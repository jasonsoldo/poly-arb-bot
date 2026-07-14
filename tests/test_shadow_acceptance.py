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
        "strategy_counts": {
            "paired_lock": {"evaluations": 10, "accepts": 2, "rejections": 8, "model_evaluations": 0},
            "late_window_directional_ev": {"evaluations": 20, "accepts": 1, "rejections": 19, "model_evaluations": 20},
            "low_price_lottery_ev": {"evaluations": 20, "accepts": 0, "rejections": 20, "model_evaluations": 20},
        },
        "strategy_latest": {
            "late_window_directional_ev": {"estimated_probability": 0.6},
            "low_price_lottery_ev": {"estimated_probability": 0.6},
            "paired_lock": {"locked_profit": -0.1},
        },
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


def test_acceptance_fails_when_an_independent_strategy_is_not_running():
    status = valid_status()
    status["strategy_counts"]["low_price_lottery_ev"] = {"evaluations": 0, "accepts": 0, "rejections": 0, "model_evaluations": 0}
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert "three_strategy_evaluations" in failed


def test_acceptance_fails_when_probability_model_never_evaluated():
    status = valid_status()
    status["strategy_counts"]["late_window_directional_ev"]["model_evaluations"] = 0
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert "probability_models_evaluated" in failed
