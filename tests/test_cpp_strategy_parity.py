import json
import os
import shutil
import subprocess
from pathlib import Path

from poly_arb_bot.cpp_strategy_parity import assert_parity, run_cpp
from poly_arb_bot.profitability_analysis import (
    probability_bucket,
    seconds_to_close_bucket,
)


ROOT = Path(__file__).parents[1]
HEADER = ROOT / "cpp/strategy/ev_strategy.hpp"
RUNNER = ROOT / "cpp/strategy/ev_strategy_test.cpp"
FIXTURES = ROOT / "tests/fixtures/strategy_parity.json"


def compiler():
    windows = Path("C:/msys64/ucrt64/bin/g++.exe")
    if windows.exists():
        return str(windows)
    return shutil.which("g++")


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
