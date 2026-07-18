import json
import time

import poly_arb_bot.shadow_acceptance as shadow_acceptance
from poly_arb_bot.shadow_acceptance import evaluate_status


def valid_status():
    return {
        "clob_readiness": {"discovered_markets": 4, "paired_markets_ready": 3, "not_ready": 1},
        "shadow_report": {
            "evaluations": 10, "accepted_evaluations": 2, "rejected_evaluations": 8,
            "rejection_reasons": {"no_edge": 6, "depth": 2}, "duplicate_events": 0,
        },
        "counts": {"executed_orders": 0},
        "shadow_execution": {"real_order_submissions": 0, "real_orders": 0, "real_fills": 0},
        "shadow_lifecycle": {"real_order_submissions": 0, "real_orders": 0, "real_fills": 0},
        "probability_observations": {
            "pending": 2, "settled": 10, "orphaned": 0,
            "semantics": "CALIBRATION_ONLY_NOT_ORDERS_OR_PNL",
        },
        "arbitrage_research": {
            "semantics": "RESEARCH_ONLY_NOT_ORDERS_OR_PNL",
            "no_repeatable_arbitrage": True,
            "conclusion": "NO REPEATABLE ARBITRAGE FOUND",
            "funnels": {
                "paired_lock": {
                    "shadow_attempts": 3, "leg_1_book_executable": 3,
                    "both_legs_book_executable": 2, "orphaned": 1,
                    "invalidated": 0,
                },
            },
        },
        "strategy_counts": {
            "paired_lock": {"evaluations": 10, "accepts": 2, "rejections": 8, "model_evaluations": 0},
            "late_window_directional_ev": {"evaluations": 20, "accepts": 1, "rejections": 19, "model_evaluations": 20, "latest_model_evaluated": True,
                                            "terminal_hedge_evaluations": 20, "terminal_hedge_accepts": 1,
                                            "terminal_hedge_rejections": 19},
            "low_price_lottery_ev": {"evaluations": 20, "accepts": 0, "rejections": 20, "model_evaluations": 20, "latest_model_evaluated": True},
            "split_sell_lock": {
                "evaluations": 20, "accepts": 1, "rejections": 19,
                "model_evaluations": 0, "latest_model_evaluated": False,
            },
            "inventory_rebalancing_arb": {
                "evaluations": 20, "accepts": 1, "rejections": 19,
                "model_evaluations": 20, "latest_model_evaluated": True,
            },
            "maker_complete_set_arb": {
                "evaluations": 20, "accepts": 0, "rejections": 20,
                "model_evaluations": 20, "latest_model_evaluated": True,
            },
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
            "split_sell_lock": {"completed": 0},
            "inventory_rebalancing_arb": {"completed": 0},
            "maker_complete_set_arb": {"completed": 0},
        },
        "shadow_health": {
            "ws_connected": True,
            "stale": False,
            "reference_connected": True,
            "reference_protocol_errors": 0,
            "strategy_audit_backpressure": 0,
            "reference_ipc_receive_age_ms_p95": 5.0,
            "reference_ipc_receive_age_samples": 100,
            "clob_to_strategy_evaluation_us_p95": 100.0,
            "clob_to_strategy_evaluation_samples": 100,
            "engine_started_at": time.time() - 3600,
            "ws_session_id": 2,
            "full_resyncs": 2,
        },
    }


def test_acceptance_passes_all_shadow_invariants():
    report = evaluate_status(valid_status())
    assert report["passed"] is True
    assert report["status"] == "PASS"
    assert all(check["passed"] for check in report["checks"])


def test_acceptance_rejects_synthetic_fill_or_mixed_probability_semantics():
    status = valid_status()
    status["arbitrage_research"]["funnels"]["paired_lock"]["both_legs_filled"] = 2
    status["probability_observations"]["semantics"] = "ORDERS"

    report = evaluate_status(status)

    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert failed == {"arbitrage_book_evidence_integrity", "probability_observation_integrity"}
    assert report["status"] == "FAIL"


def test_acceptance_reports_repeatable_arbitrage_evidence_without_calling_it_profit():
    status = valid_status()
    status["arbitrage_research"].update(
        no_repeatable_arbitrage=False,
        conclusion="REPEATABLE ARBITRAGE CANDIDATE FOUND",
    )

    report = evaluate_status(status)

    assert report["status"] == "PASS"
    assert report["metrics"]["arbitrage_research_conclusion"] == "REPEATABLE ARBITRAGE CANDIDATE FOUND"


def test_acceptance_fails_drift_and_real_order_submission():
    status = valid_status()
    status["clob_readiness"]["not_ready"] = 2
    status["shadow_report"]["rejection_reasons"]["no_edge"] = 5
    status["shadow_execution"]["real_order_submissions"] = 1
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert report["passed"] is False
    assert failed == {"market_readiness", "evaluation_reasons", "real_execution_disabled"}


def test_acceptance_fails_any_real_order_or_fill_counter():
    for section, field in (
        ("shadow_execution", "real_orders"),
        ("shadow_execution", "real_fills"),
        ("shadow_lifecycle", "real_fills"),
    ):
        status = valid_status()
        status[section][field] = 1
        report = evaluate_status(status)
        failed = {check["name"] for check in report["checks"] if not check["passed"]}
        assert "real_execution_disabled" in failed


def test_acceptance_fails_when_real_execution_invariant_is_missing():
    for section, field in (
        ("shadow_execution", "real_order_submissions"),
        ("shadow_execution", "real_orders"),
        ("shadow_execution", "real_fills"),
        ("shadow_lifecycle", "real_order_submissions"),
        ("shadow_lifecycle", "real_orders"),
        ("shadow_lifecycle", "real_fills"),
    ):
        status = valid_status()
        del status[section][field]
        report = evaluate_status(status)
        failed = {check["name"] for check in report["checks"] if not check["passed"]}
        assert "real_execution_disabled" in failed

    status = valid_status()
    del status["counts"]["executed_orders"]
    report = evaluate_status(status)
    assert "real_execution_disabled" in {
        check["name"] for check in report["checks"] if not check["passed"]
    }


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


def test_acceptance_ignores_retired_terminal_hedge_counter():
    status = valid_status()
    status["strategy_counts"]["late_window_directional_ev"]["terminal_hedge_evaluations"] = 0

    report = evaluate_status(status)

    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert failed == set()
    assert report["status"] == "PASS"


def test_acceptance_marks_missing_complete_set_strategy_evaluations_incomplete():
    status = valid_status()
    status["strategy_counts"]["maker_complete_set_arb"]["evaluations"] = 0

    report = evaluate_status(status)

    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert failed == {"complete_set_strategies_evaluated"}
    assert report["status"] == "INCOMPLETE"


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


def test_acceptance_fails_disconnected_or_corrupt_low_latency_path():
    status = valid_status()
    status["shadow_health"].update(
        ws_connected=False,
        reference_connected=False,
        reference_protocol_errors=1,
        strategy_audit_backpressure=1,
    )
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert {
        "clob_websocket_connected", "reference_ipc_connected",
        "reference_protocol_integrity", "strategy_audit_no_backpressure",
    } <= failed
    assert report["status"] == "FAIL"


def test_acceptance_fails_observed_latency_over_budget():
    status = valid_status()
    status["shadow_health"]["reference_ipc_receive_age_ms_p95"] = 51
    status["shadow_health"]["clob_to_strategy_evaluation_us_p95"] = 5001
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert failed == {"low_latency_budget"}
    assert report["metrics"]["reference_ipc_receive_age_ms_p95"] == 51
    assert report["metrics"]["clob_to_strategy_evaluation_us_p95"] == 5001


def test_acceptance_marks_missing_latency_samples_incomplete():
    status = valid_status()
    status["shadow_health"].update(
        reference_ipc_receive_age_ms_p95=None,
        reference_ipc_receive_age_samples=0,
        clob_to_strategy_evaluation_us_p95=None,
        clob_to_strategy_evaluation_samples=0,
    )
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert failed == {"low_latency_observed"}
    assert report["status"] == "INCOMPLETE"


def test_acceptance_fails_reconnect_or_book_resync_storm(monkeypatch):
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)
    status = valid_status()
    status["shadow_health"].update(
        engine_started_at=now - 3600,
        ws_session_id=14,
        full_resyncs=61,
    )
    report = evaluate_status(status)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert failed == {"websocket_stability_budget"}
    assert report["status"] == "FAIL"
    assert report["metrics"]["ws_reconnects_per_hour"] >= 13
    assert report["metrics"]["book_resyncs_per_hour"] > 60


def test_acceptance_defers_stability_rate_during_startup():
    status = valid_status()
    status["shadow_health"].update(
        engine_started_at=time.time() - 30,
        ws_session_id=4,
        full_resyncs=100,
    )
    report = evaluate_status(status)
    assert report["status"] == "PASS"
    assert report["metrics"]["ws_reconnects_per_hour"] is None


def test_acceptance_marks_missing_market_and_audit_data_incomplete():
    status = valid_status()
    status["clob_readiness"] = {"discovered_markets": 0, "paired_markets_ready": 0, "not_ready": 0}
    status["shadow_report"].update(evaluations=0, accepted_evaluations=0, rejected_evaluations=0,
                                    rejection_reasons={})
    for row in status["strategy_counts"].values():
        row.update(evaluations=0, accepts=0, rejections=0)
    report = evaluate_status(status)
    assert report["status"] == "INCOMPLETE"


def test_acceptance_waits_for_background_analytics(monkeypatch, capsys):
    warming = valid_status()
    warming["analytics_refreshing"] = True
    warming["strategy_counts"] = {}
    ready = valid_status()
    ready["analytics_refreshing"] = False
    responses = iter((warming, warming, ready))
    calls = []

    def fake_build_status(*args):
        calls.append(args)
        return next(responses)

    monkeypatch.setattr(shadow_acceptance, "build_status", fake_build_status)

    exit_code = shadow_acceptance.run(analytics_timeout_seconds=1, analytics_poll_seconds=0)

    report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert report["status"] == "PASS"
    assert len(calls) == 3


def test_acceptance_reports_analytics_timeout_as_incomplete(monkeypatch, capsys):
    warming = valid_status()
    warming["analytics_refreshing"] = True
    warming["strategy_counts"] = {}
    monkeypatch.setattr(shadow_acceptance, "build_status", lambda *args: warming)

    exit_code = shadow_acceptance.run(analytics_timeout_seconds=0, analytics_poll_seconds=0)

    report = json.loads(capsys.readouterr().out)
    failed = {check["name"] for check in report["checks"] if not check["passed"]}
    assert exit_code == 2
    assert report["status"] == "INCOMPLETE"
    assert "analytics_ready" in failed
