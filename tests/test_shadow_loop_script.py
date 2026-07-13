from pathlib import Path


SCRIPT = Path("scripts/run_shadow_loop.sh").read_text(encoding="utf-8")


def test_shadow_loop_creates_audit_log_before_network_startup():
    touch = SCRIPT.index("touch logs/shadow-audit.jsonl")
    scan = SCRIPT.index("scan_once()")
    assert touch < scan


def test_shadow_loop_runs_cpp_engine_with_structured_audit_path():
    assert "./build/market_ws_engine" in SCRIPT
    assert "logs/shadow-audit.jsonl" in SCRIPT


def test_shadow_loop_uses_project_virtualenv_under_systemd():
    assert 'python_bin="${PYTHON_BIN:-$PWD/.venv/bin/python}"' in SCRIPT
    assert '"$python_bin" -m poly_arb_bot.cli scan-updown' in SCRIPT
    assert "PYTHON_NOT_EXECUTABLE" in SCRIPT


def test_shadow_loop_starts_cpp_reference_price_engine():
    assert "./build/reference_price_engine data/venue-status.json" in SCRIPT
    assert 'kill "$scanner_pid" "$reference_pid"' in SCRIPT


def test_shadow_loop_starts_shadow_execution_state_machine():
    assert '"$python_bin" -m poly_arb_bot.shadow_execution' in SCRIPT
    assert '"$execution_pid"' in SCRIPT


def test_shadow_loop_scans_all_supported_timeframes():
    assert "--intervals 5m,15m,1h,4h" in SCRIPT
    deploy = Path("deploy/VPS_DEPLOY.md").read_text(encoding="utf-8")
    assert deploy.count("--intervals 5m,15m,1h,4h") >= 2


def test_systemd_requires_ntp_and_logrotate_retains_thirty_days():
    service = Path("deploy/poly-arb-bot.service").read_text(encoding="utf-8")
    ntp = Path("scripts/check_ntp.sh").read_text(encoding="utf-8")
    rotation = Path("deploy/poly-arb-bot.logrotate").read_text(encoding="utf-8")
    assert "ExecStartPre=/bin/bash /opt/poly-arb-bot/scripts/check_ntp.sh" in service
    assert "NTPSynchronized" in ntp
    assert "rotate 30" in rotation
    assert "copytruncate" in rotation
