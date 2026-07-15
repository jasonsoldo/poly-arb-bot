import json
import math
import subprocess

from .ev_shadow import _up_probability_model
from .ev_strategies import DirectionalInput, evaluate_directional, evaluate_lottery
from .reference_layer import ReferenceState


def python_result(case):
    if case["mode"] == "probability":
        asset = {
            "consensus_price": case["consensus_price"],
            "volatility_per_sqrt_second": case["volatility_per_sqrt_second"],
            "model_sample_count": case["model_sample_count"],
            "model_sample_span_seconds": case["model_sample_span_seconds"],
            "momentum_bps_30s": case["momentum_bps_30s"],
        }
        probability, _ = _up_probability_model(
            asset, case["price_to_beat"], case["seconds_to_close"],
            case["paired_book_imbalance"],
        )
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
