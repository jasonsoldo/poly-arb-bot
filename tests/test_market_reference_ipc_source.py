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
    assert "async_read_until" in source
    assert "MAX_FRAME_BYTES" in source
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
        "test_malformed_frame_is_discarded",
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
