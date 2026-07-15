#include "reference_snapshot.hpp"

#include <cassert>
#include <functional>
#include <iostream>

using reference_ipc::AnchorSample;
using reference_ipc::decode_line;
using reference_ipc::encode_line;
using reference_ipc::Snapshot;
using reference_ipc::SourceSnapshot;

void expect_failure(const std::function<void()>& callback) {
    bool failed = false;
    try { callback(); } catch (const std::exception&) { failed = true; }
    assert(failed);
}

Snapshot sample() {
    Snapshot input;
    input.producer_session = "session-a";
    input.sequence = 7;
    input.produced_monotonic_ns = 100;
    input.produced_wall_ms = 200;
    auto& asset = input.assets["BTC"];
    asset.revision = 9;
    asset.fast_price = 64001;
    asset.consensus_price = 64000;
    asset.settlement_reference = 63999;
    asset.cross_source_divergence_bps = 1.2;
    asset.volatility_per_sqrt_second = .0001;
    asset.momentum_bps_30s = 2.5;
    asset.clock_skew_ms = 12.5;
    asset.model_sample_count = 120;
    asset.model_sample_span_seconds = 119;
    asset.reference_quorum_met = true;
    asset.fresh_exchange_source_count = 4;
    asset.fresh_usd_spot_source_count = 2;
    SourceSnapshot source;
    source.symbol = "BTC-USD";
    source.market_type = "spot";
    source.quote_currency = "USD";
    source.status = "FRESH";
    source.price = 64000;
    source.bid = 63999;
    source.ask = 64001;
    source.message_age_ms = 4;
    source.anchor_samples.push_back(AnchorSample{1000, 1001, 63000, "5m"});
    asset.sources["coinbase"] = source;
    return input;
}

void test_round_trip() {
    const auto input = sample();
    const auto output = decode_line(encode_line(input));
    assert(output.sequence == 7);
    assert(output.producer_session == "session-a");
    assert(output.assets.at("BTC").consensus_price == 64000);
    assert(output.assets.at("BTC").revision == 9);
    assert(output.assets.at("BTC").clock_skew_ms == 12.5);
    assert(output.assets.at("BTC").sources.at("coinbase").status == "FRESH");
    assert(output.assets.at("BTC").sources.at("coinbase").anchor_samples.size() == 1);
}

void test_rejects_missing_version() {
    expect_failure([] { reference_ipc::decode_line("{\"producer_session\":\"x\",\"sequence\":1,\"produced_wall_ms\":1,\"assets\":{}}"); });
}

void test_rejects_missing_session() {
    auto input = sample(); input.producer_session.clear();
    expect_failure([&] { reference_ipc::encode_line(input); });
}

void test_rejects_zero_sequence() {
    auto input = sample(); input.sequence = 0;
    expect_failure([&] { reference_ipc::encode_line(input); });
}

void test_rejects_zero_asset_revision() {
    auto input = sample(); input.assets["BTC"].revision = 0;
    expect_failure([&] { reference_ipc::encode_line(input); });
}

void test_rejects_excess_anchors() {
    auto input = sample();
    auto& rows = input.assets["BTC"].sources["coinbase"].anchor_samples;
    while (rows.size() <= reference_ipc::MAX_ANCHORS_PER_SOURCE) rows.push_back({1, 1, 1, "5m"});
    expect_failure([&] { reference_ipc::encode_line(input); });
}

void test_rejects_unknown_status() {
    auto input = sample(); input.assets["BTC"].sources["coinbase"].status = "MAYBE";
    expect_failure([&] { reference_ipc::encode_line(input); });
}

void test_rejects_malformed_json() {
    expect_failure([] { reference_ipc::decode_line("{not-json}"); });
}

void test_transport_age_includes_queue_and_post_receive_delay() {
    assert(reference_ipc::transport_age_ms(1'000'000, 6'000'000, 2.5) == 7.5);
    assert(reference_ipc::transport_age_ms(0, 6'000'000, 2.5) == 1e9);
    assert(reference_ipc::transport_age_ms(7'000'000, 6'000'000, 2.5) == 1e9);
}

int main() {
    test_round_trip();
    test_rejects_missing_version();
    test_rejects_missing_session();
    test_rejects_zero_sequence();
    test_rejects_zero_asset_revision();
    test_rejects_excess_anchors();
    test_rejects_unknown_status();
    test_rejects_malformed_json();
    test_transport_age_includes_queue_and_post_receive_delay();
    std::cout << "reference snapshot protocol tests passed\n";
}
