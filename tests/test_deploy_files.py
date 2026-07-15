from pathlib import Path


SCRIPT = Path("scripts/run_shadow_loop.sh").read_text(encoding="utf-8")
DEPLOY = Path("deploy/VPS_DEPLOY.md").read_text(encoding="utf-8")
ROTATE = Path("deploy/poly-arb-bot.logrotate").read_text(encoding="utf-8")
BUILD_SH = Path("scripts/build_cpp.sh").read_text(encoding="utf-8")
BUILD_PS1 = Path("scripts/build_cpp.ps1").read_text(encoding="utf-8")


def test_shadow_loop_removes_stale_reference_socket_and_waits_for_ready():
    remove = SCRIPT.index('rm -f "$reference_socket"')
    reference = SCRIPT.index("./build/reference_price_engine")
    market = SCRIPT.index("./build/market_ws_engine")
    assert remove < reference < market
    assert '[[ -S "$reference_socket" ]]' in SCRIPT
    assert "REFERENCE_IPC_NOT_READY" in SCRIPT


def test_deploy_guide_has_parity_and_real_order_cutover_gates():
    assert "strategy-parity.jsonl" in DEPLOY
    assert "strategy_parity_mismatch" in DEPLOY
    assert "real_order_submissions" in DEPLOY
    assert "real_orders" in DEPLOY
    assert "real_fills" in DEPLOY
    assert "shadow-acceptance" in DEPLOY
    assert "ROLLBACK" in DEPLOY


def test_deploy_guide_documents_local_pipeline_performance_gates():
    assert "reference IPC receive age p95 < 50 ms" in DEPLOY
    assert "CLOB mutation to strategy evaluation p95 < 250 us" in DEPLOY
    assert "Web process" in DEPLOY and "80%" in DEPLOY
    assert "200 KiB/s" in DEPLOY
    assert "audit_backpressure" in DEPLOY


def test_deploy_guide_validates_units_and_official_integrations():
    assert "systemd-analyze verify" in DEPLOY
    assert "gamma-api.polymarket.com" in DEPLOY
    assert "clob.polymarket.com" in DEPLOY
    assert "data-api.binance.vision" in DEPLOY
    assert "shadow-acceptance" in DEPLOY


def test_logrotate_covers_all_runtime_logs():
    for name in (
        "shadow-audit.jsonl", "strategy-audit.jsonl", "shadow-execution.jsonl",
        "strategy-parity.jsonl", "orders.jsonl",
    ):
        assert name in ROTATE
    assert "/var/log/poly-arb-bot.log" in ROTATE
    assert "/var/log/poly-arb-web.log" in ROTATE


def test_cpp_build_scripts_run_strategy_tests_and_build_production_engines():
    assert "./build/ev_strategy_test" in BUILD_SH
    assert "build\\ev_strategy_test.exe" in BUILD_PS1
    for source in ("market_ws_engine", "reference_price_engine"):
        assert f"cpp/{source}/{source}.cpp" in BUILD_SH
        assert f"cpp/{source}/{source}.cpp" in BUILD_PS1


def test_windows_build_fails_on_native_compile_or_test_error():
    assert "function Assert-NativeSuccess" in BUILD_PS1
    assert BUILD_PS1.count("Assert-NativeSuccess") >= 11


def test_strategy_parity_smoke_closes_stdin_in_build_scripts():
    assert "printf '%s\\n' \"$strategy_smoke\" | ./build/ev_strategy_test" in BUILD_SH
    assert "$strategySmoke | & .\\build\\ev_strategy_test.exe" in BUILD_PS1
    for script in (BUILD_SH, BUILD_PS1):
        assert '"estimated_probability"' in script
        assert '"settlement_reference":101' in script
        assert '"estimated_probability":[0-9]' in script
