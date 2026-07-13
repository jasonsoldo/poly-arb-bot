from pathlib import Path


SCRIPT = Path("scripts/run_shadow_loop.sh").read_text(encoding="utf-8")


def test_shadow_loop_creates_audit_log_before_network_startup():
    touch = SCRIPT.index("touch logs/shadow-audit.jsonl")
    scan = SCRIPT.index("scan_once()")
    assert touch < scan


def test_shadow_loop_runs_cpp_engine_with_structured_audit_path():
    assert "./build/market_ws_engine" in SCRIPT
    assert "logs/shadow-audit.jsonl" in SCRIPT
