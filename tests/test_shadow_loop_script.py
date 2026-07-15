from pathlib import Path


SCRIPT = Path("scripts/run_shadow_loop.sh").read_text(encoding="utf-8")


def test_shadow_loop_creates_audit_log_before_network_startup():
    touch = SCRIPT.index("touch logs/shadow-audit.jsonl")
    scan = SCRIPT.index("scan_once()")
    assert touch < scan
    assert "touch logs/shadow-execution.jsonl" in SCRIPT


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


def test_shadow_loop_starts_cpp_strategy_parity_verifier():
    assert '"$python_bin" -m poly_arb_bot.ev_shadow' in SCRIPT
    assert "EV_SHADOW_MODE=verify" in SCRIPT
    assert "logs/strategy-parity.jsonl" in SCRIPT
    assert 'ev_pid=$!' in SCRIPT
    assert '"$ev_pid"' in SCRIPT


def test_shadow_loop_scans_all_supported_timeframes():
    assert "--intervals 5m,15m,1h,4h" in SCRIPT
    deploy = Path("deploy/VPS_DEPLOY.md").read_text(encoding="utf-8")
    assert deploy.count("--intervals 5m,15m,1h,4h") >= 2


def test_shadow_loop_enforces_scan_deadline_without_replacing_old_markets():
    assert 'scan_deadline_seconds="${SCAN_DEADLINE_SECONDS:-45}"' in SCRIPT
    assert 'timeout --signal=TERM "$scan_deadline_seconds"' in SCRIPT
    assert "SHADOW_LOOP scan_deadline_or_error" in SCRIPT
    assert "use_retained_markets" in SCRIPT


def test_shadow_loop_schedules_scan_at_next_market_boundary():
    assert "market_refresh_delay" in SCRIPT
    assert 'scan_delay="$(next_scan_delay 2>/dev/null || printf' in SCRIPT
    assert 'sleep "$scan_delay"' in SCRIPT


def test_systemd_requires_ntp_and_logrotate_retains_thirty_days():
    service = Path("deploy/poly-arb-bot.service").read_text(encoding="utf-8")
    ntp = Path("scripts/check_ntp.sh").read_text(encoding="utf-8")
    rotation = Path("deploy/poly-arb-bot.logrotate").read_text(encoding="utf-8")
    assert "ExecStartPre=/bin/bash /opt/poly-arb-bot/scripts/check_ntp.sh" in service
    assert "NTPSynchronized" in ntp
    assert "rotate 30" in rotation
    assert "copytruncate" in rotation
    assert "maxsize 256M" in rotation
    assert "delaycompress" not in rotation
    env = Path("deploy/env.example").read_text(encoding="utf-8")
    assert "STRATEGY_ACCEPT_AUDIT_HEARTBEAT_SECONDS=5" in env
    assert "STRATEGY_REJECT_AUDIT_HEARTBEAT_SECONDS=60" in env
    deploy = Path("deploy/VPS_DEPLOY.md").read_text(encoding="utf-8")
    assert "sudo cp deploy/poly-arb-bot.logrotate /etc/logrotate.d/poly-arb-bot" in deploy
    assert "sudo logrotate -d /etc/logrotate.d/poly-arb-bot" in deploy
