from pathlib import Path


HEADER = Path("cpp/reference_ipc/reference_snapshot.hpp")
CPP_TEST = Path("cpp/reference_ipc/reference_snapshot_test.cpp")
BUILD_SH = Path("scripts/build_cpp.sh").read_text(encoding="utf-8")
BUILD_PS1 = Path("scripts/build_cpp.ps1").read_text(encoding="utf-8")


def test_reference_ipc_protocol_is_versioned_and_session_scoped():
    source = HEADER.read_text(encoding="utf-8")
    for field in (
        "protocol_version",
        "producer_session",
        "sequence",
        "produced_monotonic_ns",
        "produced_wall_ms",
    ):
        assert field in source
    assert "PROTOCOL_VERSION = 2" in source
    assert "unsupported reference protocol version" in source
    assert "reference producer session missing" in source
    assert "reference sequence must be positive" in source


def test_reference_ipc_carries_compact_strategy_and_source_state():
    source = HEADER.read_text(encoding="utf-8")
    for field in (
        "revision",
        "fast_price",
        "consensus_price",
        "settlement_reference",
        "cross_source_divergence_bps",
        "volatility_per_sqrt_second",
        "momentum_bps_30s",
        "model_sample_count",
        "model_sample_span_seconds",
        "reference_quorum_met",
        "fresh_exchange_source_count",
        "fresh_usd_spot_source_count",
        "anchor_samples",
        "settlement_samples",
        "message_age_ms",
        "market_type",
        "quote_currency",
        "status",
    ):
        assert field in source
    assert "MAX_ANCHORS_PER_SOURCE = 8" in source
    assert "reference anchor limit exceeded" in source


def test_reference_ipc_has_real_round_trip_and_invalid_frame_tests():
    test_source = CPP_TEST.read_text(encoding="utf-8")
    for case in (
        "test_round_trip",
        "test_rejects_missing_version",
        "test_rejects_missing_session",
        "test_rejects_zero_sequence",
        "test_rejects_zero_asset_revision",
        "test_rejects_excess_anchors",
        "test_rejects_unknown_status",
        "test_rejects_malformed_json",
    ):
        assert case in test_source
    assert "decode_line(encode_line(input))" in test_source


def test_reference_ipc_test_binary_is_built_on_linux_and_windows():
    assert "reference_snapshot_test.cpp" in BUILD_SH
    assert "build/reference_snapshot_test" in BUILD_SH
    assert "reference_snapshot_test.cpp" in BUILD_PS1
    assert "build/reference_snapshot_test.exe" in BUILD_PS1
