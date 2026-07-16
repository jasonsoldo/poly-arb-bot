from pathlib import Path


SERVER = Path("cpp/reference_ipc/latest_value_server.hpp")
SERVER_TEST = Path("cpp/reference_ipc/latest_value_server_test.cpp")
ENGINE = Path("cpp/reference_price_engine/reference_price_engine.cpp").read_text(encoding="utf-8")
BUILD_SH = Path("scripts/build_cpp.sh").read_text(encoding="utf-8")
BUILD_PS1 = Path("scripts/build_cpp.ps1").read_text(encoding="utf-8")


def test_reference_server_is_async_and_latest_value_only():
    source = SERVER.read_text(encoding="utf-8")
    assert "class LatestValueServer" in source
    assert "boost::asio::local::stream_protocol" in source
    assert "async_accept" in source
    assert "async_write" in source
    assert "pending_frame_" in source
    assert "pending_frame_ = std::move(frame)" in source
    assert "std::deque" not in source
    assert "latest_frame_" in source
    assert "remove(socket_path" in source


def test_reference_server_has_real_connect_coalescing_and_cleanup_tests():
    source = SERVER_TEST.read_text(encoding="utf-8")
    for case in (
        "test_client_receives_latest_snapshot_on_connect",
        "test_slow_client_keeps_only_latest_pending_frame",
        "test_disconnected_client_is_removed",
        "test_socket_path_is_cleaned_up",
    ):
        assert case in source


def test_reference_engine_publishes_ipc_and_slows_only_diagnostic_file():
    assert '#include "../reference_ipc/latest_value_server.hpp"' in ENGINE
    assert '#include "../reference_ipc/reference_snapshot.hpp"' in ENGINE
    assert "REFERENCE_IPC_PATH" in ENGINE
    assert "reference_publisher" in ENGINE
    assert "reference_publisher->publish" in ENGINE
    assert "STATUS_WRITE_INTERVAL_MS = 1000" in ENGINE
    assert "IPC_PUBLISH_INTERVAL_MS = 20" in ENGINE


def test_reference_engine_separates_system_clock_skew_from_message_transport_age():
    assert "system_clock_skew_ms" in ENGINE
    assert "adjtimex" in ENGINE
    assert 'std::getenv("CLOCK_SKEW_MS")' in ENGINE
    assert "system_ntp_offset" in ENGINE
    assert "std::abs(source.received_at - *state.source_timestamp_ms)" not in ENGINE
    assert "std::abs(source.received_at - source_ms)" not in ENGINE


def test_reference_server_test_binary_is_built_on_linux_and_windows():
    for script in (BUILD_SH, BUILD_PS1):
        assert "latest_value_server_test.cpp" in script
        assert "latest_value_server_test" in script
