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
        "shadow_lifecycle": {"real_order_submissions": 0, "real_orders": 0},
        "strategy_counts": {
            "paired_lock": {"evaluations": 10, "accepts": 2, "rejections": 8, "model_evaluations": 0},
            "late_window_directional_ev": {"evaluations": 20, "accepts": 1, "rejections": 19, "model_evaluations": 20, "latest_model_evaluated": True},
            "low_price_lottery_ev": {"evaluations": 20, "accepts": 0, "rejections": 20, "model_evaluations": 20, "latest_model_evaluated": True},
        },
        "strategy_latest": {
            "late_window_directional_ev": {"estimated_probability": 0.6},
            "low_price_lottery_ev": {"estimated_probability": 0.6},
            "paired_lock": {"locked_profit": -0.1},
        },
        "performance": {"completed": 0},
        "performance_by_strategy": {
            "late_window_directional_ev": {"completed": 0},
            "low_price_lottery_ev": {"completed": 0},
            "paired_lock": {"completed": 0},
        },
    }


def test_acceptance_passes_all_shadow_invariants():
    report = evaluate_status(valid_status())
    assert report["passed"] is True
    assert report["status"] == "PASS"
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
    status["strategy_counts"]["late_window_directional_ev"]["latest_model_evaluated"] = False
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert "probability_models_evaluated" in failed


def test_acceptance_uses_model_count_when_latest_row_fails_closed_before_model():
    status = valid_status()
    status["strategy_counts"]["late_window_directional_ev"]["latest_model_evaluated"] = False
    status["strategy_counts"]["low_price_lottery_ev"]["latest_model_evaluated"] = False
    report = evaluate_status(status)
    assert report["status"] == "PASS"


def test_acceptance_allows_zero_completed_samples_when_evaluations_are_valid():
    status = valid_status()
    report = evaluate_status(status)
    assert report["status"] == "PASS"


def test_acceptance_marks_missing_market_and_audit_data_incomplete():
    status = valid_status()
    status["clob_readiness"] = {"discovered_markets": 0, "paired_markets_ready": 0, "not_ready": 0}
    status["shadow_report"].update(evaluations=0, accepted_evaluations=0, rejected_evaluations=0,
                                    rejection_reasons={})
    for row in status["strategy_counts"].values():
        row.update(evaluations=0, accepts=0, rejections=0)
    report = evaluate_status(status)
    assert report["status"] == "INCOMPLETE"
