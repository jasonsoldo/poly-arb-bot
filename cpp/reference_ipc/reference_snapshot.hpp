#pragma once

#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>

#include <cmath>
#include <cstdint>
#include <iomanip>
#include <map>
#include <optional>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace reference_ipc {

constexpr int PROTOCOL_VERSION = 1;
constexpr std::size_t MAX_ANCHORS_PER_SOURCE = 8;

struct AnchorSample {
    double source_timestamp_ms = 0;
    double received_at_ms = 0;
    double price = 0;
    std::string timeframe;
};

struct SourceSnapshot {
    std::string symbol;
    std::string market_type;
    std::string quote_currency;
    std::string status = "NOT_RECEIVED";
    std::optional<double> price;
    std::optional<double> bid;
    std::optional<double> ask;
    std::optional<double> source_timestamp_ms;
    std::optional<double> received_at_ms;
    std::optional<double> message_age_ms;
    std::vector<AnchorSample> anchor_samples;
    std::vector<AnchorSample> settlement_samples;
};

struct AssetSnapshot {
    std::optional<double> fast_price;
    std::optional<double> consensus_price;
    std::optional<double> settlement_reference;
    std::optional<double> cross_source_divergence_bps;
    std::optional<double> volatility_per_sqrt_second;
    std::optional<double> momentum_bps_30s;
    std::optional<double> clock_skew_ms;
    int model_sample_count = 0;
    double model_sample_span_seconds = 0;
    bool reference_quorum_met = false;
    int fresh_exchange_source_count = 0;
    int fresh_usd_spot_source_count = 0;
    std::map<std::string, SourceSnapshot> sources;
};

struct Snapshot {
    int protocol_version = PROTOCOL_VERSION;
    std::string producer_session;
    std::uint64_t sequence = 0;
    std::uint64_t produced_monotonic_ns = 0;
    double produced_wall_ms = 0;
    std::map<std::string, AssetSnapshot> assets;
};

inline bool known_status(const std::string& status) {
    return status == "FRESH" || status == "STALE" || status == "DISCONNECTED" ||
           status == "NOT_RECEIVED" || status == "UNSUPPORTED" || status == "OUTLIER";
}

inline void validate(const Snapshot& snapshot) {
    if (snapshot.protocol_version != PROTOCOL_VERSION)
        throw std::runtime_error("unsupported reference protocol version");
    if (snapshot.producer_session.empty())
        throw std::runtime_error("reference producer session missing");
    if (snapshot.sequence == 0)
        throw std::runtime_error("reference sequence must be positive");
    if (!std::isfinite(snapshot.produced_wall_ms) || snapshot.produced_wall_ms <= 0)
        throw std::runtime_error("reference wall timestamp invalid");
    for (const auto& asset : snapshot.assets) {
        for (const auto& source : asset.second.sources) {
            if (!known_status(source.second.status))
                throw std::runtime_error("unknown reference source status");
            if (source.second.anchor_samples.size() > MAX_ANCHORS_PER_SOURCE ||
                source.second.settlement_samples.size() > MAX_ANCHORS_PER_SOURCE)
                throw std::runtime_error("reference anchor limit exceeded");
        }
    }
}

inline std::string escaped(const std::string& value) {
    std::ostringstream out;
    for (const unsigned char ch : value) {
        switch (ch) {
            case '"': out << "\\\""; break;
            case '\\': out << "\\\\"; break;
            case '\b': out << "\\b"; break;
            case '\f': out << "\\f"; break;
            case '\n': out << "\\n"; break;
            case '\r': out << "\\r"; break;
            case '\t': out << "\\t"; break;
            default:
                if (ch < 0x20) {
                    out << "\\u" << std::hex << std::setw(4) << std::setfill('0')
                        << static_cast<int>(ch) << std::dec;
                } else {
                    out << ch;
                }
        }
    }
    return out.str();
}

inline void write_optional(std::ostream& out, const std::optional<double>& value) {
    if (value && std::isfinite(*value)) out << *value;
    else out << "null";
}

inline void write_anchors(std::ostream& out, const std::vector<AnchorSample>& rows) {
    out << '[';
    bool first = true;
    for (const auto& row : rows) {
        if (!first) out << ',';
        first = false;
        out << "{\"source_timestamp_ms\":" << row.source_timestamp_ms
            << ",\"received_at_ms\":" << row.received_at_ms
            << ",\"price\":" << row.price
            << ",\"timeframe\":\"" << escaped(row.timeframe) << "\"}";
    }
    out << ']';
}

inline std::string encode_line(const Snapshot& snapshot) {
    validate(snapshot);
    std::ostringstream out;
    out << std::setprecision(15)
        << "{\"protocol_version\":" << snapshot.protocol_version
        << ",\"producer_session\":\"" << escaped(snapshot.producer_session)
        << "\",\"sequence\":" << snapshot.sequence
        << ",\"produced_monotonic_ns\":" << snapshot.produced_monotonic_ns
        << ",\"produced_wall_ms\":" << snapshot.produced_wall_ms
        << ",\"assets\":{";
    bool first_asset = true;
    for (const auto& asset : snapshot.assets) {
        if (!first_asset) out << ',';
        first_asset = false;
        const auto& row = asset.second;
        out << '"' << escaped(asset.first) << "\":{\"fast_price\":";
        write_optional(out, row.fast_price);
        out << ",\"consensus_price\":"; write_optional(out, row.consensus_price);
        out << ",\"settlement_reference\":"; write_optional(out, row.settlement_reference);
        out << ",\"cross_source_divergence_bps\":"; write_optional(out, row.cross_source_divergence_bps);
        out << ",\"volatility_per_sqrt_second\":"; write_optional(out, row.volatility_per_sqrt_second);
        out << ",\"momentum_bps_30s\":"; write_optional(out, row.momentum_bps_30s);
        out << ",\"clock_skew_ms\":"; write_optional(out, row.clock_skew_ms);
        out << ",\"model_sample_count\":" << row.model_sample_count
            << ",\"model_sample_span_seconds\":" << row.model_sample_span_seconds
            << ",\"reference_quorum_met\":" << (row.reference_quorum_met ? "true" : "false")
            << ",\"fresh_exchange_source_count\":" << row.fresh_exchange_source_count
            << ",\"fresh_usd_spot_source_count\":" << row.fresh_usd_spot_source_count
            << ",\"sources\":{";
        bool first_source = true;
        for (const auto& source : row.sources) {
            if (!first_source) out << ',';
            first_source = false;
            const auto& state = source.second;
            out << '"' << escaped(source.first) << "\":{\"symbol\":\"" << escaped(state.symbol)
                << "\",\"market_type\":\"" << escaped(state.market_type)
                << "\",\"quote_currency\":\"" << escaped(state.quote_currency)
                << "\",\"status\":\"" << escaped(state.status) << "\",\"price\":";
            write_optional(out, state.price);
            out << ",\"bid\":"; write_optional(out, state.bid);
            out << ",\"ask\":"; write_optional(out, state.ask);
            out << ",\"source_timestamp_ms\":"; write_optional(out, state.source_timestamp_ms);
            out << ",\"received_at_ms\":"; write_optional(out, state.received_at_ms);
            out << ",\"message_age_ms\":"; write_optional(out, state.message_age_ms);
            out << ",\"anchor_samples\":"; write_anchors(out, state.anchor_samples);
            out << ",\"settlement_samples\":"; write_anchors(out, state.settlement_samples);
            out << '}';
        }
        out << "}}";
    }
    out << "}}\n";
    return out.str();
}

inline std::optional<double> optional_number(
        const boost::property_tree::ptree& row, const std::string& key) {
    const auto value = row.get_optional<std::string>(key);
    if (!value || value->empty() || *value == "null") return std::nullopt;
    try {
        const double number = std::stod(*value);
        if (!std::isfinite(number)) return std::nullopt;
        return number;
    } catch (...) {
        throw std::runtime_error("invalid reference number: " + key);
    }
}

inline std::vector<AnchorSample> read_anchors(
        const boost::property_tree::ptree& row, const std::string& key) {
    std::vector<AnchorSample> result;
    if (const auto child = row.get_child_optional(key)) {
        for (const auto& item : *child) {
            result.push_back({
                item.second.get<double>("source_timestamp_ms"),
                item.second.get<double>("received_at_ms"),
                item.second.get<double>("price"),
                item.second.get<std::string>("timeframe", ""),
            });
        }
    }
    return result;
}

inline Snapshot decode_line(std::string_view line) {
    try {
        std::stringstream input{std::string(line)};
        boost::property_tree::ptree root;
        boost::property_tree::read_json(input, root);
        Snapshot snapshot;
        snapshot.protocol_version = root.get<int>("protocol_version", 0);
        snapshot.producer_session = root.get<std::string>("producer_session", "");
        snapshot.sequence = root.get<std::uint64_t>("sequence", 0);
        snapshot.produced_monotonic_ns = root.get<std::uint64_t>("produced_monotonic_ns", 0);
        snapshot.produced_wall_ms = root.get<double>("produced_wall_ms", 0);
        if (const auto assets = root.get_child_optional("assets")) {
            for (const auto& item : *assets) {
                AssetSnapshot asset;
                const auto& row = item.second;
                asset.fast_price = optional_number(row, "fast_price");
                asset.consensus_price = optional_number(row, "consensus_price");
                asset.settlement_reference = optional_number(row, "settlement_reference");
                asset.cross_source_divergence_bps = optional_number(row, "cross_source_divergence_bps");
                asset.volatility_per_sqrt_second = optional_number(row, "volatility_per_sqrt_second");
                asset.momentum_bps_30s = optional_number(row, "momentum_bps_30s");
                asset.clock_skew_ms = optional_number(row, "clock_skew_ms");
                asset.model_sample_count = row.get<int>("model_sample_count", 0);
                asset.model_sample_span_seconds = row.get<double>("model_sample_span_seconds", 0);
                asset.reference_quorum_met = row.get<bool>("reference_quorum_met", false);
                asset.fresh_exchange_source_count = row.get<int>("fresh_exchange_source_count", 0);
                asset.fresh_usd_spot_source_count = row.get<int>("fresh_usd_spot_source_count", 0);
                if (const auto sources = row.get_child_optional("sources")) {
                    for (const auto& source_item : *sources) {
                        const auto& source_row = source_item.second;
                        SourceSnapshot source;
                        source.symbol = source_row.get<std::string>("symbol", "");
                        source.market_type = source_row.get<std::string>("market_type", "");
                        source.quote_currency = source_row.get<std::string>("quote_currency", "");
                        source.status = source_row.get<std::string>("status", "NOT_RECEIVED");
                        source.price = optional_number(source_row, "price");
                        source.bid = optional_number(source_row, "bid");
                        source.ask = optional_number(source_row, "ask");
                        source.source_timestamp_ms = optional_number(source_row, "source_timestamp_ms");
                        source.received_at_ms = optional_number(source_row, "received_at_ms");
                        source.message_age_ms = optional_number(source_row, "message_age_ms");
                        source.anchor_samples = read_anchors(source_row, "anchor_samples");
                        source.settlement_samples = read_anchors(source_row, "settlement_samples");
                        asset.sources[source_item.first] = std::move(source);
                    }
                }
                snapshot.assets[item.first] = std::move(asset);
            }
        }
        validate(snapshot);
        return snapshot;
    } catch (const std::runtime_error&) {
        throw;
    } catch (const std::exception& error) {
        throw std::runtime_error(std::string("malformed reference frame: ") + error.what());
    }
}

}  // namespace reference_ipc
