import json
import os
import shutil
import subprocess
from pathlib import Path

from poly_arb_bot.cpp_strategy_parity import assert_parity, run_cpp
from poly_arb_bot.probability_calibration_map import PROBABILITY_MODEL_IDS
from poly_arb_bot.profitability_analysis import (
    cohort_key,
    probability_bucket,
    seconds_to_close_bucket,
)
from poly_arb_bot.profitability_gate import (
    build_profitability_gate,
    canonical_payload_hash,
)


ROOT = Path(__file__).parents[1]
HEADER = ROOT / "cpp/strategy/ev_strategy.hpp"
RUNNER = ROOT / "cpp/strategy/ev_strategy_test.cpp"
FIXTURES = ROOT / "tests/fixtures/strategy_parity.json"
PROFITABILITY_RUNNER = ROOT / "cpp/strategy/profitability_gate_boundary_test.cpp"
NOW = 1_000.0
BASE_HASHES = {
    "late_window_directional_ev": "directional-base",
    "low_price_lottery_ev": "lottery-base",
}


def compiler():
    windows = Path("C:/msys64/ucrt64/bin/g++.exe")
    if windows.exists():
        return str(windows)
    return shutil.which("g++")


def _calibration_snapshot():
    stats = {
        "samples": 100,
        "expected_up_rate": 0.85,
        "realized_up_rate": 0.80,
    }
    payload = {
        "version": 2,
        "generated_at": NOW - 1,
        "config": {"min_bucket_samples": 30, "prior_weight": 30.0},
        "excluded_other_cohort": {
            "late_window_directional_ev": 0,
            "low_price_lottery_ev": 0,
        },
        "strategies": {
            strategy: {
                "cohort": {
                    "strategy_config_hash": BASE_HASHES[strategy],
                    "probability_model_id": PROBABILITY_MODEL_IDS[strategy],
                },
                "timeframes": {"5m": {"0.8-0.9": dict(stats)}},
                "overall": {"0.8-0.9": dict(stats)},
            }
            for strategy in BASE_HASHES
        },
        "validation_activated_at": NOW,
        "validation_expires_at": NOW + 72 * 3600,
    }
    payload["content_hash"] = canonical_payload_hash(payload)
    return payload


def _candidate(asset="BTC"):
    return {
        "strategy": "late_window_directional_ev",
        "asset": asset,
        "timeframe": "5m",
        "outcome": "Up",
        "calibration_input_probability": 0.85,
        "expected_fill_price": 0.45,
        "seconds_to_close": 45,
    }


def _cohort(markets=60):
    row = _candidate()
    return {
        "dimensions": {
            "strategy": row["strategy"],
            "asset": row["asset"],
            "timeframe": row["timeframe"],
            "outcome": row["outcome"],
            "probability": probability_bucket(
                row["calibration_input_probability"]
            ),
            "fill": probability_bucket(row["expected_fill_price"]),
            "seconds": seconds_to_close_bucket(row["seconds_to_close"]),
        },
        "independent_markets": markets,
        "mean_net_return": 0.05,
        "net_pnl_usd": 12.0,
        "lower_bound_95": 0.01,
        "largest_positive_market_share": 0.10,
    }


def _gate(snapshot):
    eligible = _candidate()
    rejected = _candidate(asset="ETH")
    report = {
        "version": 1,
        "generated_at": NOW,
        "source": {},
        "selected_config_hashes": {
            "late_window_directional_ev": BASE_HASHES[
                "late_window_directional_ev"
            ],
        },
        "blocking_exclusions": {},
        "cohorts": {
            cohort_key(eligible): _cohort(),
            cohort_key(rejected): {
                **_cohort(markets=49),
                "dimensions": {
                    **_cohort()["dimensions"],
                    "asset": "ETH",
                },
            },
        },
        "real_order_submissions": 0,
        "real_orders": 0,
        "real_fills": 0,
    }
    return build_profitability_gate(
        report,
        snapshot,
        {
            "late_window_directional_ev": BASE_HASHES[
                "late_window_directional_ev"
            ],
        },
        NOW,
        "1",
    )


def _compile_profitability_runner(tmp_path):
    cxx = compiler()
    assert cxx, "g++ is required for C++ profitability boundary tests"
    binary = tmp_path / (
        "profitability_gate_boundary_test.exe"
        if Path(cxx).suffix == ".exe"
        else "profitability_gate_boundary_test"
    )
    subprocess.run(
        [
            cxx, "-std=c++17", "-O2", "-static", "-static-libgcc",
            "-static-libstdc++", "-I", str(ROOT), str(PROFITABILITY_RUNNER),
            "-o", str(binary),
        ],
        check=True, cwd=ROOT,
    )
    return binary


def _write_artifact(path, payload, *, sort_keys=False):
    path.write_text(
        json.dumps(payload, sort_keys=sort_keys, separators=(",", ":")),
        encoding="utf-8",
    )


def _self_hash(payload):
    payload["content_hash"] = canonical_payload_hash(payload)
    return payload


def _run_validation(
    binary, tmp_path, snapshot, gate, now=NOW + 1, evaluation_now=None,
):
    snapshot_path = tmp_path / "snapshot.json"
    gate_path = tmp_path / "gate.json"
    _write_artifact(snapshot_path, snapshot)
    _write_artifact(gate_path, gate)
    command = [
        str(binary), "validate", str(snapshot_path), str(gate_path),
        str(now), "1",
        BASE_HASHES["late_window_directional_ev"],
        BASE_HASHES["low_price_lottery_ev"],
    ]
    if evaluation_now is not None:
        command.append(str(evaluation_now))
    completed = subprocess.run(
        command,
        check=True, text=True, capture_output=True,
    )
    return completed.stdout.splitlines()


def test_cpp_strategy_has_independent_models_and_fail_closed_gates():
    source = HEADER.read_text(encoding="utf-8")
    assert "probability_model" in source
    assert "lottery_probability_model" in source
    assert "lottery_market_blend_probability" in source
    assert "evaluate_directional" in source
    assert "evaluate_lottery" in source
    # The helper remains covered as library math, but the market engine no longer
    # combines the two probability strategies into a runtime hedge strategy.
    assert "outside_time_window" in source
    assert "directional_enforce_time_window" in source
    assert "model_confidence_below_threshold" in source
    assert "directional_min_probability" in source
    assert "entry_price_above_limit" in source
    assert "settlement_reference_unverified" in source


def test_cpp_profitability_cohort_helper_matches_python(tmp_path):
    cxx = compiler()
    assert cxx, "g++ is required for C++ profitability cohort parity"
    source = tmp_path / "profitability_gate_test.cpp"
    source.write_text(
        """
#include "cpp/strategy/profitability_gate.hpp"
#include <iostream>
#include <set>
int main() {
    profitability_gate::Candidate candidate{
        "late_window_directional_ev", "BTC", "5m", "Up", 0.85, 0.45, 45
    };
    const auto result = profitability_gate::evaluate(
        candidate, true, std::set<std::string>{
            "strategy=late_window_directional_ev|asset=BTC|timeframe=5m|"
            "outcome=Up|probability=0.8-0.9|fill=0.4-0.5|seconds=30-60"
        });
    std::cout << result.decision << "\\n" << result.reason << "\\n"
              << result.cohort_key << "\\n";
    for (double value : {0.0, 0.0999, 0.1, 0.999, 1.0})
        std::cout << profitability_gate::decile_bucket(value) << "\\n";
    for (double value : {
             0.0, 29.999, 30.0, 59.999, 60.0, 89.999, 90.0,
             179.999, 180.0, 299.999, 300.0, 599.999, 600.0})
        std::cout << profitability_gate::seconds_bucket(value) << "\\n";
}
""".strip(),
        encoding="utf-8",
    )
    binary = tmp_path / (
        "profitability_gate_test.exe" if Path(cxx).suffix == ".exe"
        else "profitability_gate_test"
    )
    subprocess.run(
        [
            cxx, "-std=c++17", "-O2", "-static", "-static-libgcc",
            "-static-libstdc++", "-I", str(ROOT), str(source), "-o", str(binary),
        ],
        check=True, cwd=ROOT,
    )
    completed = subprocess.run(
        [str(binary)], check=True, text=True, capture_output=True,
    )
    assert completed.stdout.splitlines() == [
        "ALLOW",
        "profitability_cohort_eligible",
        (
            "strategy=late_window_directional_ev|asset=BTC|timeframe=5m|"
            "outcome=Up|probability=0.8-0.9|fill=0.4-0.5|seconds=30-60"
        ),
        *(
            probability_bucket(value)
            for value in (0.0, 0.0999, 0.1, 0.999, 1.0)
        ),
        *(
            seconds_to_close_bucket(value)
            for value in (
                0.0, 29.999, 30.0, 59.999, 60.0, 89.999, 90.0,
                179.999, 180.0, 299.999, 300.0, 599.999, 600.0,
            )
        ),
    ]


def test_cpp_canonical_payload_matches_python_after_recursive_key_reordering(
    tmp_path,
):
    binary = _compile_profitability_runner(tmp_path)
    payload = _gate(_calibration_snapshot())
    artifact = tmp_path / "reordered.json"
    _write_artifact(artifact, payload, sort_keys=False)

    completed = subprocess.run(
        [str(binary), "canonical", str(artifact)],
        check=True, text=True, capture_output=True,
    )
    canonical, digest = completed.stdout.splitlines()
    expected_payload = {
        key: value for key, value in payload.items() if key != "content_hash"
    }

    assert canonical == json.dumps(
        expected_payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    )
    assert digest == canonical_payload_hash(payload)


def test_cpp_profitability_artifacts_allow_only_valid_eligible_cohort(tmp_path):
    binary = _compile_profitability_runner(tmp_path)
    snapshot = _calibration_snapshot()
    gate = _gate(snapshot)

    assert _run_validation(binary, tmp_path, snapshot, gate)[:4] == [
        "READY",
        "",
        "ALLOW",
        "profitability_cohort_eligible",
    ]


def test_cpp_profitability_matching_rechecks_both_expiries_each_evaluation(
    tmp_path,
):
    binary = _compile_profitability_runner(tmp_path)
    snapshot = _calibration_snapshot()
    gate = _gate(snapshot)
    immediate_expiry = gate["validation_expires_at"]

    before = _run_validation(
        binary, tmp_path, snapshot, gate, now=NOW + 1,
        evaluation_now=immediate_expiry - 0.001,
    )
    expired = _run_validation(
        binary, tmp_path, snapshot, gate, now=NOW + 1,
        evaluation_now=immediate_expiry,
    )

    assert before[0] == "READY"
    assert before[2] == "ALLOW"
    assert expired[0] == "READY"
    assert expired[2] == "BLOCK"
    assert expired[3] == "profitability_gate_unavailable"


def test_cpp_rejects_self_hashed_deeply_malformed_calibration_snapshots(
    tmp_path,
):
    binary = _compile_profitability_runner(tmp_path)
    valid_snapshot = _calibration_snapshot()
    valid_gate = _gate(valid_snapshot)
    mutations = []

    missing_field = json.loads(json.dumps(valid_snapshot))
    del missing_field["strategies"]["late_window_directional_ev"][
        "overall"
    ]["0.8-0.9"]["realized_up_rate"]
    mutations.append(missing_field)

    arbitrary_bucket = json.loads(json.dumps(valid_snapshot))
    arbitrary_bucket["strategies"]["late_window_directional_ev"][
        "overall"
    ]["bogus"] = arbitrary_bucket["strategies"][
        "late_window_directional_ev"
    ]["overall"].pop("0.8-0.9")
    mutations.append(arbitrary_bucket)

    zero_samples = json.loads(json.dumps(valid_snapshot))
    zero_samples["strategies"]["late_window_directional_ev"][
        "overall"
    ]["0.8-0.9"]["samples"] = 0
    mutations.append(zero_samples)

    invalid_rate = json.loads(json.dumps(valid_snapshot))
    invalid_rate["strategies"]["late_window_directional_ev"][
        "overall"
    ]["0.8-0.9"]["realized_up_rate"] = 1.1
    mutations.append(invalid_rate)

    invalid_identity = json.loads(json.dumps(valid_snapshot))
    invalid_identity["strategies"]["late_window_directional_ev"]["cohort"][
        "probability_model_id"
    ] = "wrong"
    mutations.append(invalid_identity)

    invalid_base_identity = json.loads(json.dumps(valid_snapshot))
    invalid_base_identity["strategies"]["late_window_directional_ev"][
        "cohort"
    ]["strategy_config_hash"] = "wrong"
    mutations.append(invalid_base_identity)

    invalid_strategy_schema = json.loads(json.dumps(valid_snapshot))
    invalid_strategy_schema["strategies"]["unknown"] = (
        invalid_strategy_schema["strategies"].pop(
            "late_window_directional_ev"
        )
    )
    mutations.append(invalid_strategy_schema)

    invalid_config = json.loads(json.dumps(valid_snapshot))
    invalid_config["config"]["min_bucket_samples"] = 0
    mutations.append(invalid_config)

    unusable_overall = json.loads(json.dumps(valid_snapshot))
    unusable_overall["strategies"]["late_window_directional_ev"][
        "overall"
    ]["0.8-0.9"]["samples"] = 1
    mutations.append(unusable_overall)

    for mutation in mutations:
        _self_hash(mutation)
        gate = json.loads(json.dumps(valid_gate))
        gate["calibration_snapshot_hash"] = mutation["content_hash"]
        _self_hash(gate)
        assert _run_validation(binary, tmp_path, mutation, gate)[0] == (
            "BLOCKED"
        )


def test_cpp_rejects_self_hashed_malformed_or_inconsistent_gates(tmp_path):
    binary = _compile_profitability_runner(tmp_path)
    snapshot = _calibration_snapshot()
    valid_gate = _gate(snapshot)
    mutations = []

    inconsistent_decision = json.loads(json.dumps(valid_gate))
    inconsistent_decision["decision"] = "NO_TRADE"
    mutations.append(inconsistent_decision)

    bad_eligible = json.loads(json.dumps(valid_gate))
    eligible = next(iter(bad_eligible["eligible_cohorts"].values()))
    eligible["independent_markets"] = "60"
    mutations.append(bad_eligible)

    bad_rejected = json.loads(json.dumps(valid_gate))
    rejected = next(iter(bad_rejected["rejected_cohorts"].values()))
    rejected["decision"] = "ALLOW"
    mutations.append(bad_rejected)

    bad_threshold = json.loads(json.dumps(valid_gate))
    bad_threshold["thresholds"]["minimum_independent_markets"] = 1
    mutations.append(bad_threshold)

    bad_identity = json.loads(json.dumps(valid_gate))
    bad_identity["target_base_config_hashes"][
        "late_window_directional_ev"
    ] = "wrong"
    mutations.append(bad_identity)

    bad_model_identity = json.loads(json.dumps(valid_gate))
    bad_model_identity["probability_model_ids"][
        "late_window_directional_ev"
    ] = "wrong"
    mutations.append(bad_model_identity)

    bad_discovery_identity = json.loads(json.dumps(valid_gate))
    bad_discovery_identity["source_discovery_config_hashes"][
        "late_window_directional_ev"
    ] = "wrong"
    mutations.append(bad_discovery_identity)

    allow_without_eligible = json.loads(json.dumps(valid_gate))
    allow_without_eligible["eligible_cohorts"] = {}
    mutations.append(allow_without_eligible)

    for mutation in mutations:
        _self_hash(mutation)
        lines = _run_validation(binary, tmp_path, snapshot, mutation)
        assert lines[0] == "BLOCKED"
        assert lines[2] == "BLOCK"


def test_cpp_matches_python_strategy_results(tmp_path):
    cxx = compiler()
    assert cxx, "g++ is required for C++ strategy parity"
    binary = tmp_path / ("ev_strategy_test.exe" if Path(cxx).suffix == ".exe" else "ev_strategy_test")
    environment = os.environ.copy()
    if Path(cxx).drive:
        environment["PATH"] = "C:/msys64/ucrt64/bin;C:/msys64/usr/bin;" + environment.get("PATH", "")
    subprocess.run([
        cxx, "-std=c++17", "-O2", "-DBOOST_BIND_GLOBAL_PLACEHOLDERS",
        str(RUNNER), "-o", str(binary),
    ], check=True, cwd=ROOT, env=environment)
    cases = json.loads(FIXTURES.read_text(encoding="utf-8"))
    old_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = environment["PATH"]
        assert_parity(cases, run_cpp(binary, cases))
    finally:
        os.environ["PATH"] = old_path
