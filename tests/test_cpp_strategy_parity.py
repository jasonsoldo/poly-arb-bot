import json
import os
import shutil
import subprocess
from pathlib import Path

from poly_arb_bot.cpp_strategy_parity import assert_parity, run_cpp


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
    assert "outside_time_window" in source
    assert "entry_price_above_limit" in source
    assert "settlement_reference_unverified" in source


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
