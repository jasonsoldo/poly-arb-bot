import json
import math
import subprocess

from .ev_shadow import (
    _lottery_market_blend_probability,
    _lottery_up_probability_model,
    _up_probability_model,
)
from .ev_strategies import DirectionalInput, evaluate_directional, evaluate_lottery
from .reference_layer import ReferenceState


def python_result(case):
    if case["mode"] == "terminal_hedge":
        main_unit_cost = (
            case["main_expected_fill_price"] + case["main_fee_per_share"]
            + case["main_slippage_per_share"] + .003 + .002
        )
        hedge_unit_cost = (
            case["hedge_expected_fill_price"] + case["hedge_fee_per_share"]
            + case["hedge_slippage_per_share"] + .01 + .005
        )
        main_cost = case["main_size"] * main_unit_cost
        hedge_size = hedge_cost = total_cost = 0.0
        main_win_pnl = reversal_pnl = expected_pnl = 0.0
        reason = None
        if case["hedge_expected_fill_price"] > .05:
            reason = "hedge_price_above_limit"
        elif not case["hedge_target_depth_ok"] or case["hedge_liquidity"] < case["hedge_minimum_liquidity"]:
            reason = "hedge_depth_insufficient"
        elif case["hedge_slippage_per_share"] > case["hedge_maximum_slippage"]:
            reason = "hedge_slippage_exceeded"
        elif hedge_unit_cost >= 1:
            reason = "hedge_unit_cost_invalid"
        else:
            hedge_size = max(0.0, (main_cost - 1.0) / (1 - hedge_unit_cost))
            if hedge_size <= 0:
                reason = "hedge_not_required"
            elif hedge_size > case["main_size"]:
                reason = "hedge_size_above_limit"
            else:
                hedge_cost = hedge_size * hedge_unit_cost
                total_cost = main_cost + hedge_cost
                main_win_pnl = case["main_size"] - total_cost
                reversal_pnl = hedge_size - total_cost
                expected_pnl = (
                    case["main_probability"] * main_win_pnl
                    + (1 - case["main_probability"]) * reversal_pnl
                )
                if main_win_pnl <= 0:
                    reason = "main_win_pnl_not_positive"
                elif reversal_pnl < -1.0 - 1e-9:
                    reason = "reversal_loss_above_limit"
                elif expected_pnl < .05:
                    reason = "portfolio_ev_below_threshold"
                else:
                    reason = "terminal_hedged_opportunity"
        accepted = reason == "terminal_hedged_opportunity"
        return {
            "accepted": accepted, "reason": reason, "hedge_size": hedge_size,
            "main_cost": main_cost, "hedge_cost": hedge_cost, "total_cost": total_cost,
            "main_win_pnl": main_win_pnl, "reversal_pnl": reversal_pnl,
            "expected_pnl": expected_pnl,
        }
    if case["mode"] in {"probability", "lottery_probability"}:
        asset = {
            "settlement_reference": case["settlement_reference"],
            "volatility_per_sqrt_second": case["volatility_per_sqrt_second"],
            "model_sample_count": case["model_sample_count"],
            "model_sample_span_seconds": case["model_sample_span_seconds"],
            "momentum_bps_30s": case["momentum_bps_30s"],
        }
        model = (_lottery_up_probability_model
                 if case["mode"] == "lottery_probability" else _up_probability_model)
        probability, _ = model(
            asset, case["price_to_beat"], case["seconds_to_close"],
            case["paired_book_imbalance"],
        )
        if case["mode"] == "lottery_probability":
            return {
                "raw_estimated_probability": probability,
                "estimated_probability": _lottery_market_blend_probability(
                    probability, case["market_implied_probability"],
                ),
            }
        return {"estimated_probability": probability}

    ready = case.get("reference_quorum_met", True)
    reference = ReferenceState(
        [], 101, 100, 100, 2 if ready else 1, 1, 10, ready,
        "REFERENCE_READY" if ready else "REFERENCE_BLOCKED",
        case.get("reference_block_reason"),
    )
    row = DirectionalInput(
        strategy=case["strategy"], market_id="m1", condition_id="c1",
        asset="BTC", timeframe=case["timeframe"], outcome="Up",
        market_price=case["expected_fill_price"],
        expected_fill_price=case["expected_fill_price"],
        estimated_probability=case.get("estimated_probability"),
        seconds_to_close=case["seconds_to_close"],
        price_to_beat=case.get("price_to_beat", 100), reference=reference,
        fee_per_share=case.get("fee_per_share", .01),
        slippage_per_share=case.get("slippage_per_share", .002),
        latency_risk_buffer=.003, settlement_risk_buffer=.002,
        model_uncertainty_buffer=.01, execution_risk_buffer=.005,
        liquidity=case.get("liquidity", 100),
        book_age_ms=case.get("book_age_ms", 50), reference_age_ms=50,
        clock_skew_ms=10, minimum_liquidity=20, maximum_slippage=.01,
        maximum_reference_age_ms=3000, maximum_book_age_ms=750,
        maximum_clock_skew_ms=250, market_active=True, market_tradable=True,
        target_depth_ok=case.get("target_depth_ok", True), momentum_bps_30s=2,
        order_book_imbalance=.1, confidence=.8, settlement_source_verified=True,
        probability_block_reason=case.get("probability_block_reason"),
    )
    decision = evaluate_directional(row) if case["strategy"] == "late_window_directional_ev" else evaluate_lottery(row)
    return {
        "decision": decision.decision,
        "reason": decision.reason,
        "gross_edge": decision.gross_edge,
        "net_ev": decision.net_ev,
    }


def run_cpp(binary, cases):
    payload = "".join(json.dumps(case, separators=(",", ":")) + "\n" for case in cases)
    completed = subprocess.run(
        [str(binary)], input=payload, text=True, capture_output=True, check=True,
    )
    return [json.loads(line) for line in completed.stdout.splitlines()]


def assert_parity(cases, actual):
    assert len(cases) == len(actual)
    for case, cpp in zip(cases, actual):
        expected = python_result(case)
        for key, value in expected.items():
            if isinstance(value, float):
                assert math.isclose(cpp[key], value, rel_tol=0, abs_tol=1e-12), (case["name"], key)
            else:
                assert cpp[key] == value, (case["name"], key)
