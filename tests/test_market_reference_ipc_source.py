from pathlib import Path


CLIENT = Path("cpp/reference_ipc/latest_value_client.hpp")
CLIENT_TEST = Path("cpp/reference_ipc/latest_value_client_test.cpp")
ENGINE = Path("cpp/market_ws_engine/market_ws_engine.cpp").read_text(encoding="utf-8")
BUILD_SH = Path("scripts/build_cpp.sh").read_text(encoding="utf-8")
BUILD_PS1 = Path("scripts/build_cpp.ps1").read_text(encoding="utf-8")


def test_reference_client_is_async_bounded_and_reconnecting():
    source = CLIENT.read_text(encoding="utf-8")
    assert "class LatestValueClient" in source
    assert "async_connect" in source
    assert "async_read_some" in source
    assert "MAX_FRAME_BYTES" in source
    assert "READ_BUFFER_BYTES = 64 * 1024" in source
    assert "COALESCE_WINDOW{1}" in source
    assert "coalesced_frames_" in source
    assert "reconnect_timer_" in source
    assert "sequence rollback" in source
    assert "producer_session" in source
    assert "protocol_errors_" in source
    assert "reconnects_" in source


def test_reference_client_has_real_framing_and_session_tests():
    source = CLIENT_TEST.read_text(encoding="utf-8")
    for case in (
        "test_fragmented_frame",
        "test_combined_frames",
        "test_large_frame_burst_keeps_up_with_latest_value",
        "test_malformed_frame_invalidates_burst",
        "test_latest_malformed_frame_is_discarded",
        "test_oversized_completed_frame_invalidates_connection",
        "test_sequence_rollback_invalidates_connection",
        "test_new_producer_session_is_accepted",
        "test_eof_reconnects",
    ):
        assert case in source


def test_market_engine_consumes_reference_without_coupling_paired_lock():
    assert '#include "../reference_ipc/latest_value_client.hpp"' in ENGINE
    assert "REFERENCE_IPC_PATH" in ENGINE
    assert "reference_client_" in ENGINE
    assert "on_reference_snapshot" in ENGINE
    assert "reference_connected" in ENGINE
    assert "reference_sequence" in ENGINE
    assert "reference_producer_session" in ENGINE
    assert "reference_protocol_errors" in ENGINE
    assert "reference_reconnects" in ENGINE
    assert "reference_receive_age_ms" in ENGINE
    paired_section = ENGINE.split("void evaluate()", 1)[1].split("void write_health", 1)[0]
    assert "reference_connected_" not in paired_section


def test_reference_client_test_binary_is_built_on_linux_and_windows():
    for script in (BUILD_SH, BUILD_PS1):
        assert "latest_value_client_test.cpp" in script
        assert "latest_value_client_test" in script


def test_windows_production_engines_are_statically_linked():
    for source in ("market_ws_engine.cpp", "reference_price_engine.cpp"):
        command = BUILD_PS1.split(source, 1)[0].rsplit("g++", 1)[1]
        assert "-static -static-libgcc -static-libstdc++" in command
