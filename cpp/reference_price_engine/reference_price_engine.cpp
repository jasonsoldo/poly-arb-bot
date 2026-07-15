#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>
#include "../reference_ipc/latest_value_server.hpp"
#include "../reference_ipc/reference_snapshot.hpp"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <memory>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
using tcp = asio::ip::tcp;
using ssl_socket = asio::ssl::stream<tcp::socket>;
using boost::property_tree::ptree;

double now_ms() {
    return std::chrono::duration<double, std::milli>(
        std::chrono::system_clock::now().time_since_epoch()).count();
}

struct SourceState {
    std::string symbol;
    std::string market_type;
    std::string quote_currency;
    std::string source_timestamp;
    double price = 0;
    double bid = 0;
    double ask = 0;
    double received_at = 0;
    bool connected = false;
    bool supported = false;
    std::deque<std::pair<double, double>> samples;
    struct AnchorSample { double source_timestamp_ms, received_at, price; std::string timeframe; };
    std::deque<AnchorSample> anchor_samples;
    std::deque<AnchorSample> settlement_samples;
};

struct AssetState {
    std::uint64_t revision = 1;
    std::map<std::string, SourceState> sources;
};

struct CoinbaseBook {
    std::map<double, double> bids;
    std::map<double, double> asks;
};

void set_coinbase_level(std::map<double, double>& levels, double price, double size) {
    if (price <= 0 || size < 0) return;
    if (size == 0) levels.erase(price); else levels[price] = size;
}

bool apply_coinbase_book_message(
    const ptree& row,
    std::map<std::string, CoinbaseBook>& books,
    std::string& symbol,
    double& bid,
    double& ask
) {
    const std::string type = row.get<std::string>("type", "");
    if (type != "snapshot" && type != "l2update") return false;
    symbol = row.get<std::string>("product_id", "");
    if (symbol.empty()) return false;
    CoinbaseBook& book = books[symbol];
    if (type == "snapshot") {
        book.bids.clear();
        book.asks.clear();
        for (const auto* side : {"bids", "asks"}) {
            const auto rows = row.get_child_optional(side);
            if (!rows) continue;
            auto& levels = std::string(side) == "bids" ? book.bids : book.asks;
            for (const auto& level : *rows) {
                auto value = level.second.begin();
                if (value == level.second.end()) continue;
                const double price = value->second.get_value<double>();
                if (++value == level.second.end()) continue;
                set_coinbase_level(levels, price, value->second.get_value<double>());
            }
        }
    } else {
        const auto changes = row.get_child_optional("changes");
        if (!changes) return false;
        for (const auto& change : *changes) {
            auto value = change.second.begin();
            if (value == change.second.end()) continue;
            const std::string side = value->second.get_value<std::string>();
            if (++value == change.second.end()) continue;
            const double price = value->second.get_value<double>();
            if (++value == change.second.end()) continue;
            auto& levels = side == "buy" ? book.bids : book.asks;
            set_coinbase_level(levels, price, value->second.get_value<double>());
        }
    }
    if (book.bids.empty() || book.asks.empty()) return false;
    bid = book.bids.rbegin()->first;
    ask = book.asks.begin()->first;
    return bid > 0 && ask > bid;
}

struct AssetConfig {
    const char* asset;
    const char* binance;
    const char* chainlink;
    const char* coinbase;
    const char* kraken;
    const char* bybit;
    const char* okx;
};

const AssetConfig ASSETS[] = {
    {"BTC", "btcusdt", "btc/usd", "BTC-USD", "BTC/USD", "BTCUSDT", "BTC-USDT"},
    {"ETH", "ethusdt", "eth/usd", "ETH-USD", "ETH/USD", "ETHUSDT", "ETH-USDT"},
    {"SOL", "solusdt", "sol/usd", "SOL-USD", "SOL/USD", "SOLUSDT", "SOL-USDT"},
    {"XRP", "xrpusdt", "xrp/usd", "XRP-USD", "XRP/USD", "XRPUSDT", "XRP-USDT"},
    {"BNB", "bnbusdt", "bnb/usd", "BNB-USD", "BNB/USD", "BNBUSDT", "BNB-USDT"},
    {"DOGE", "dogeusdt", "doge/usd", "DOGE-USD", "DOGE/USD", "DOGEUSDT", "DOGE-USDT"},
    {"HYPE", "", "hype/usd", "HYPE-USD", "HYPE/USD", "", "HYPE-USDT"},
};

struct SharedState {
    std::mutex mutex;
    std::map<std::string, AssetState> assets;
    std::string output_path;
    unsigned long long matched_messages = 0;
    unsigned long long unmatched_messages = 0;
    double engine_latency_us = 0;
    double last_status_write_ms = 0;
    unsigned long long status_writes = 0;
    double last_ipc_publish_ms = 0;
    unsigned long long ipc_sequence = 0;
    std::string producer_session;
    std::shared_ptr<reference_ipc::LatestValueServer> reference_publisher;
};

constexpr double STATUS_WRITE_INTERVAL_MS = 1000;
constexpr double IPC_PUBLISH_INTERVAL_MS = 20;
constexpr double DEFAULT_REFERENCE_FRESHNESS_MS = 3000;
constexpr double COINBASE_REFERENCE_FRESHNESS_MS = 10000;
constexpr double MODEL_SAMPLE_BUCKET_MS = 1000;

double freshness_from_env(const char* name, double fallback) {
    const char* raw = std::getenv(name);
    if (!raw) return fallback;
    try {
        const double value = std::stod(raw);
        return value > 0 && std::isfinite(value) ? value : fallback;
    } catch (...) {
        return fallback;
    }
}

double source_freshness_limit_ms(const std::string& source_name) {
    static const double default_limit = freshness_from_env(
        "REFERENCE_MAX_AGE_MS", DEFAULT_REFERENCE_FRESHNESS_MS
    );
    static const double coinbase_limit = freshness_from_env(
        "COINBASE_REFERENCE_MAX_AGE_MS", COINBASE_REFERENCE_FRESHNESS_MS
    );
    return source_name == "coinbase"
        ? coinbase_limit
        : default_limit;
}

std::string source_status(
    const std::string& source_name,
    const SourceState& source,
    double timestamp
) {
    if (!source.supported) return "UNSUPPORTED";
    if (!source.connected) return source.received_at ? "DISCONNECTED" : "NOT_RECEIVED";
    if (!source.received_at) return "NOT_RECEIVED";
    return timestamp - source.received_at <= source_freshness_limit_ms(source_name)
        ? "FRESH"
        : "STALE";
}

void write_number(std::ostream& out, double value) {
    if (value > 0 && std::isfinite(value)) out << value; else out << "null";
}

double median(std::vector<double> values) {
    if (values.empty()) return 0;
    std::sort(values.begin(), values.end());
    const size_t middle = values.size() / 2;
    return values.size() % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

double volatility_per_sqrt_second(const SourceState& source) {
    if (source.samples.size() < 20) return 0;
    double sum_squares = 0;
    size_t count = 0;
    for (size_t i = 1; i < source.samples.size(); ++i) {
        const double elapsed = (source.samples[i].first - source.samples[i - 1].first) / 1000;
        if (elapsed <= 0 || source.samples[i - 1].second <= 0 || source.samples[i].second <= 0) continue;
        const double normalized = std::log(source.samples[i].second / source.samples[i - 1].second) / std::sqrt(elapsed);
        sum_squares += normalized * normalized;
        ++count;
    }
    return count ? std::sqrt(sum_squares / count) : 0;
}

double model_sample_span_seconds(const SourceState& source) {
    if (source.samples.size() < 2) return 0;
    return std::max(0.0, (source.samples.back().first - source.samples.front().first) / 1000);
}

double momentum_bps(const SourceState& source, double timestamp, double horizon_ms = 30000) {
    if (source.samples.size() < 2 || source.samples.back().second <= 0) return 0;
    auto start = source.samples.begin();
    while (start != source.samples.end() && timestamp - start->first > horizon_ms) ++start;
    if (start == source.samples.end() || start->second <= 0) return 0;
    return (source.samples.back().second / start->second - 1) * 10000;
}

void publish_ipc_locked(SharedState& shared, bool force = false) {
    const double timestamp = now_ms();
    if (!shared.reference_publisher ||
        (!force && timestamp - shared.last_ipc_publish_ms < IPC_PUBLISH_INTERVAL_MS)) return;
    shared.last_ipc_publish_ms = timestamp;
    reference_ipc::Snapshot snapshot;
    snapshot.producer_session = shared.producer_session;
    snapshot.sequence = ++shared.ipc_sequence;
    snapshot.produced_monotonic_ns = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count());
    snapshot.produced_wall_ms = timestamp;
    for (const auto& config : ASSETS) {
        const auto& asset = shared.assets.at(config.asset);
        auto& output = snapshot.assets[config.asset];
        output.revision = asset.revision;
        std::vector<double> fresh_spot, fresh_usd, volatilities, momentums;
        std::vector<double> model_sample_counts, model_sample_spans;
        std::map<std::string, std::vector<double>> quote_prices;
        for (const auto& item : asset.sources) {
            const auto& source = item.second;
            if (source_status(item.first, source, timestamp) == "FRESH" &&
                source.market_type == "spot" && source.price)
                quote_prices[source.quote_currency].push_back(source.price);
        }
        std::map<std::string, double> quote_medians;
        for (const auto& item : quote_prices) quote_medians[item.first] = median(item.second);
        const auto is_outlier = [&](const SourceState& source) {
            const auto found = quote_medians.find(source.quote_currency);
            return found != quote_medians.end() && found->second > 0 &&
                   std::abs(source.price - found->second) / found->second * 10000 > 100;
        };
        double fast_price = 0, settlement_reference = 0, clock_skew_upper_bound_ms = 0;
        for (const auto& item : asset.sources) {
            const auto& source = item.second;
            auto& state = output.sources[item.first];
            state.symbol = source.symbol;
            state.market_type = source.market_type;
            state.quote_currency = source.quote_currency;
            state.status = source_status(item.first, source, timestamp) == "FRESH" &&
                           source.market_type == "spot" && is_outlier(source)
                           ? "OUTLIER" : source_status(item.first, source, timestamp);
            if (source.price > 0) state.price = source.price;
            if (source.bid > 0) state.bid = source.bid;
            if (source.ask > 0) state.ask = source.ask;
            if (source.received_at > 0) {
                state.received_at_ms = source.received_at;
                state.message_age_ms = std::max(0.0, timestamp - source.received_at);
            }
            if (!source.source_timestamp.empty()) {
                try {
                    double source_ms = std::stod(source.source_timestamp);
                    if (source_ms > 0 && source_ms < 1e12) source_ms *= 1000;
                    if (source_ms > 0) state.source_timestamp_ms = source_ms;
                } catch (...) {}
            }
            const auto copy_anchors = [](const auto& input, auto& target) {
                const auto begin = input.size() > reference_ipc::MAX_ANCHORS_PER_SOURCE
                    ? input.end() - reference_ipc::MAX_ANCHORS_PER_SOURCE : input.begin();
                for (auto row = begin; row != input.end(); ++row)
                    target.push_back({row->source_timestamp_ms, row->received_at, row->price, row->timeframe});
            };
            copy_anchors(source.anchor_samples, state.anchor_samples);
            copy_anchors(source.settlement_samples, state.settlement_samples);
            if (state.status != "FRESH" || source.price <= 0) continue;
            if (item.first == "chainlink") {
                settlement_reference = source.price;
                continue;
            }
            if (source.market_type != "spot" || is_outlier(source)) continue;
            if (state.source_timestamp_ms && source.received_at > 0) {
                const double delta = std::abs(source.received_at - *state.source_timestamp_ms);
                if (!clock_skew_upper_bound_ms || delta < clock_skew_upper_bound_ms)
                    clock_skew_upper_bound_ms = delta;
            }
            fresh_spot.push_back(source.price);
            ++output.fresh_exchange_source_count;
            if (source.quote_currency == "USD") {
                fresh_usd.push_back(source.price);
                ++output.fresh_usd_spot_source_count;
            }
            if (!fast_price && (item.first == "binance" || item.first == "bybit" || item.first == "okx"))
                fast_price = source.price;
            const double volatility = volatility_per_sqrt_second(source);
            if (volatility > 0) {
                volatilities.push_back(volatility);
                model_sample_counts.push_back(static_cast<double>(source.samples.size()));
                model_sample_spans.push_back(model_sample_span_seconds(source));
            }
            if (source.samples.size() >= 2) momentums.push_back(momentum_bps(source, timestamp));
        }
        const double consensus = median(fresh_usd);
        double divergence = 0;
        if (fresh_spot.size() > 1) {
            const auto bounds = std::minmax_element(fresh_spot.begin(), fresh_spot.end());
            const double center = median(fresh_spot);
            if (center) divergence = (*bounds.second - *bounds.first) / center * 10000;
        }
        if (fast_price > 0) output.fast_price = fast_price;
        if (consensus > 0) output.consensus_price = consensus;
        if (settlement_reference > 0) output.settlement_reference = settlement_reference;
        if (fresh_spot.size() > 1) output.cross_source_divergence_bps = divergence;
        const double volatility = median(volatilities);
        if (volatility > 0) output.volatility_per_sqrt_second = volatility;
        if (!momentums.empty()) output.momentum_bps_30s = median(momentums);
        if (clock_skew_upper_bound_ms > 0) output.clock_skew_ms = clock_skew_upper_bound_ms;
        output.model_sample_count = static_cast<int>(median(model_sample_counts));
        output.model_sample_span_seconds = median(model_sample_spans);
        output.reference_quorum_met = output.fresh_exchange_source_count >= 2 &&
            output.fresh_usd_spot_source_count >= 1 && settlement_reference > 0 && divergence <= 100;
    }
    shared.reference_publisher->publish(reference_ipc::encode_line(snapshot));
}

void write_status_locked(SharedState& shared, bool force = false) {
    const double timestamp = now_ms();
    if (!force && timestamp - shared.last_status_write_ms < STATUS_WRITE_INTERVAL_MS) return;
    shared.last_status_write_ms = timestamp;
    ++shared.status_writes;
    const std::string temporary = shared.output_path + ".tmp";
    std::ofstream out(temporary, std::ios::trunc);
    out << std::setprecision(15) << "{\"updated_at_ms\":" << timestamp << ",\"assets\":{";
    bool first_asset = true;
    for (const auto& config : ASSETS) {
        if (!first_asset) out << ',';
        first_asset = false;
        const auto& asset = shared.assets.at(config.asset);
        std::vector<double> fresh_spot, fresh_usd;
        std::map<std::string, std::vector<double>> quote_prices;
        for (const auto& item : asset.sources) {
            const auto& source = item.second;
            if (source_status(item.first, source, timestamp) == "FRESH" && source.market_type == "spot" && source.price)
                quote_prices[source.quote_currency].push_back(source.price);
        }
        std::map<std::string, double> quote_medians;
        for (const auto& item : quote_prices) quote_medians[item.first] = median(item.second);
        const auto is_outlier = [&](const SourceState& source) {
            const auto found = quote_medians.find(source.quote_currency);
            return found != quote_medians.end() && found->second > 0 &&
                   std::abs(source.price - found->second) / found->second * 10000 > 100;
        };
        double fast_price = 0, settlement_reference = 0;
        double clock_skew_upper_bound_ms = 0;
        int fresh_sources = 0, fresh_usd_sources = 0;
        std::vector<double> volatilities, momentums;
        std::vector<double> model_sample_counts, model_sample_spans;
        for (const auto& item : asset.sources) {
            const auto& source = item.second;
            if (source_status(item.first, source, timestamp) != "FRESH" || !source.price) continue;
            if (item.first == "chainlink") {
                settlement_reference = source.price;
                continue;
            }
            if (source.market_type != "spot" || is_outlier(source)) continue;
            if (!source.source_timestamp.empty()) {
                try {
                    double source_ms = std::stod(source.source_timestamp);
                    if (source_ms > 0 && source_ms < 1e12) source_ms *= 1000;
                    const double delta = std::abs(source.received_at - source_ms);
                    if (source_ms > 0 && (!clock_skew_upper_bound_ms || delta < clock_skew_upper_bound_ms))
                        clock_skew_upper_bound_ms = delta;
                } catch (...) {}
            }
            fresh_spot.push_back(source.price);
            ++fresh_sources;
            if (source.quote_currency == "USD") { fresh_usd.push_back(source.price); ++fresh_usd_sources; }
            if (!fast_price && (item.first == "binance" || item.first == "bybit" || item.first == "okx")) fast_price = source.price;
            const double volatility = volatility_per_sqrt_second(source);
            if (volatility > 0) {
                volatilities.push_back(volatility);
                model_sample_counts.push_back(static_cast<double>(source.samples.size()));
                model_sample_spans.push_back(model_sample_span_seconds(source));
            }
            if (source.samples.size() >= 2) momentums.push_back(momentum_bps(source, timestamp));
        }
        const double consensus = median(fresh_usd);
        double divergence = 0;
        if (fresh_spot.size() > 1) {
            const auto bounds = std::minmax_element(fresh_spot.begin(), fresh_spot.end());
            const double center = median(fresh_spot);
            if (center) divergence = (*bounds.second - *bounds.first) / center * 10000;
        }
        const bool quorum = fresh_sources >= 2 && fresh_usd_sources >= 1 && settlement_reference > 0 && divergence <= 100;
        out << '\"' << config.asset << "\":{\"sources\":{";
        bool first_source = true;
        for (const auto& item : asset.sources) {
            if (!first_source) out << ',';
            first_source = false;
            const auto& source = item.second;
            const std::string status = source_status(item.first, source, timestamp) == "FRESH" &&
                                       source.market_type == "spot" && is_outlier(source)
                                       ? "OUTLIER" : source_status(item.first, source, timestamp);
            out << '\"' << item.first << "\":{\"supported\":" << (source.supported ? "true" : "false")
                << ",\"symbol\":\"" << source.symbol
                << "\",\"market_type\":\"" << source.market_type
                << "\",\"quote_currency\":\"" << source.quote_currency << "\",\"price\":";
            write_number(out, source.price); out << ",\"bid\":"; write_number(out, source.bid);
            out << ",\"ask\":"; write_number(out, source.ask);
            out << ",\"source_timestamp\":\"" << source.source_timestamp
                << "\",\"received_at\":"; write_number(out, source.received_at);
            out << ",\"message_age_ms\":";
            if (source.received_at) out << std::max(0.0, timestamp - source.received_at); else out << "null";
            out << ",\"status\":\"" << status << "\"}";
        }
        const auto& binance = asset.sources.at("binance");
        const auto& chainlink = asset.sources.at("chainlink");
        out << "},\"binance\":"; write_number(out, binance.price);
        out << ",\"chainlink\":"; write_number(out, chainlink.price);
        out << ",\"binance_status\":\"" << source_status("binance", binance, timestamp)
            << "\",\"chainlink_status\":\"" << source_status("chainlink", chainlink, timestamp) << "\""
            << ",\"binance_source_age_ms\":";
        if (binance.received_at) out << std::max(0.0, timestamp - binance.received_at); else out << -1;
        out << ",\"chainlink_source_age_ms\":";
        if (chainlink.received_at) out << std::max(0.0, timestamp - chainlink.received_at); else out << -1;
        out << ",\"fresh_exchange_source_count\":" << fresh_sources
            << ",\"fresh_usd_spot_source_count\":" << fresh_usd_sources
            << ",\"consensus_price\":"; write_number(out, consensus);
        out << ",\"fast_price\":"; write_number(out, fast_price);
        out << ",\"clock_skew_ms\":"; write_number(out, clock_skew_upper_bound_ms);
        out << ",\"clock_skew_basis\":\"minimum_reference_receive_delta_upper_bound\"";
        out << ",\"settlement_reference\":"; write_number(out, settlement_reference);
        out << ",\"cross_source_divergence_bps\":";
        if (fresh_spot.size() > 1) out << divergence; else out << "null";
        out << ",\"reference_quorum_met\":" << (quorum ? "true" : "false")
            << ",\"reference_state\":\"" << (quorum ? "REFERENCE_READY" : "REFERENCE_BLOCKED") << "\"";
        for (const auto* name : {"binance", "chainlink"}) {
            out << ",\"" << name << "_samples\":[";
            bool first_anchor = true;
            for (const auto& sample : asset.sources.at(name).anchor_samples) {
                if (!first_anchor) out << ',';
                first_anchor = false;
                out << "{\"source_timestamp_ms\":" << sample.source_timestamp_ms
                    << ",\"received_at\":" << sample.received_at
                    << ",\"price\":" << sample.price
                    << ",\"timeframe\":\"" << sample.timeframe << "\"}";
            }
            out << ']';
            out << ",\"" << name << "_settlement_samples\":[";
            first_anchor = true;
            for (const auto& sample : asset.sources.at(name).settlement_samples) {
                if (!first_anchor) out << ',';
                first_anchor = false;
                out << "{\"source_timestamp_ms\":" << sample.source_timestamp_ms
                    << ",\"received_at\":" << sample.received_at
                    << ",\"price\":" << sample.price
                    << ",\"timeframe\":\"" << sample.timeframe << "\"}";
            }
            out << ']';
        }
        out
            << ",\"volatility_per_sqrt_second\":"; write_number(out, median(volatilities));
        out << ",\"momentum_bps_30s\":";
        if (!momentums.empty()) out << median(momentums); else out << "null";
        out << ",\"model_sample_count\":" << static_cast<int>(median(model_sample_counts));
        out << ",\"model_sample_span_seconds\":";
        write_number(out, median(model_sample_spans));
        out << '}';
    }
    out << "},\"engine_latency_us\":" << shared.engine_latency_us
        << ",\"matched_messages\":" << shared.matched_messages
        << ",\"unmatched_messages\":" << shared.unmatched_messages
        << ",\"status_writes\":" << shared.status_writes << "}\n";
    out.flush(); out.close();
    std::error_code error;
    std::filesystem::rename(temporary, shared.output_path, error);
    if (error) {
        std::filesystem::remove(shared.output_path, error);
        std::filesystem::rename(temporary, shared.output_path);
    }
}

void publish(SharedState& shared, const std::string& asset, const std::string& source,
             double price, double bid, double ask, const std::string& source_timestamp) {
    const double received = now_ms();
    std::lock_guard<std::mutex> lock(shared.mutex);
    auto& row = shared.assets.at(asset).sources.at(source);
    ++shared.assets.at(asset).revision;
    row.price = price; row.bid = bid; row.ask = ask;
    row.source_timestamp = source_timestamp; row.received_at = received; row.connected = true;
    const bool same_model_bucket = !row.samples.empty() &&
        static_cast<long long>(row.samples.back().first / MODEL_SAMPLE_BUCKET_MS) ==
        static_cast<long long>(received / MODEL_SAMPLE_BUCKET_MS);
    if (same_model_bucket) {
        row.samples.back().first = received;
        row.samples.back().second = price;
    } else {
        row.samples.emplace_back(received, price);
    }
    while (!row.samples.empty() && received - row.samples.front().first > 300000) row.samples.pop_front();
    while (row.samples.size() > 512) row.samples.pop_front();
    if (source == "chainlink" && !source_timestamp.empty()) {
        try {
            double source_timestamp_ms = std::stod(source_timestamp);
            if (source_timestamp_ms > 0 && source_timestamp_ms < 1e12) source_timestamp_ms *= 1000;
            if (source_timestamp_ms > 0) {
                row.anchor_samples.push_back({source_timestamp_ms, received, price, ""});
                row.settlement_samples.push_back({source_timestamp_ms, received, price, ""});
                while (row.anchor_samples.size() > 128) row.anchor_samples.pop_front();
                while (row.settlement_samples.size() > 128) row.settlement_samples.pop_front();
            }
        } catch (...) {}
    }
    ++shared.matched_messages;
    shared.engine_latency_us = (now_ms() - received) * 1000;
    publish_ipc_locked(shared);
    write_status_locked(shared);
}

void publish_anchor(SharedState& shared, const std::string& asset, const std::string& source,
                    double price, double source_timestamp_ms, const std::string& timeframe) {
    const double received = now_ms();
    std::lock_guard<std::mutex> lock(shared.mutex);
    auto& samples = shared.assets.at(asset).sources.at(source).anchor_samples;
    ++shared.assets.at(asset).revision;
    samples.push_back({source_timestamp_ms, received, price, timeframe});
    while (samples.size() > 128) samples.pop_front();
    publish_ipc_locked(shared);
    write_status_locked(shared);
}

void publish_settlement(SharedState& shared, const std::string& asset, const std::string& source,
                        double price, double source_timestamp_ms, const std::string& timeframe) {
    const double received = now_ms();
    std::lock_guard<std::mutex> lock(shared.mutex);
    auto& samples = shared.assets.at(asset).sources.at(source).settlement_samples;
    ++shared.assets.at(asset).revision;
    samples.push_back({source_timestamp_ms, received, price, timeframe});
    while (samples.size() > 128) samples.pop_front();
    publish_ipc_locked(shared, true);
    write_status_locked(shared, true);
}

void set_connected(SharedState& shared, const std::string& source, bool connected) {
    std::lock_guard<std::mutex> lock(shared.mutex);
    for (auto& asset : shared.assets) {
        auto& row = asset.second.sources.at(source);
        if (row.supported) {
            row.connected = connected;
            ++asset.second.revision;
        }
    }
    publish_ipc_locked(shared, true);
    write_status_locked(shared, true);
}

template <typename Handler>
void websocket_loop(SharedState& shared, const std::string& source, const std::string& host,
                    const std::string& port, const std::string& path, const std::string& subscription,
                    bool text_ping, Handler handler) {
    for (;;) {
        try {
            asio::io_context io;
            asio::ssl::context ssl(asio::ssl::context::tls_client);
            ssl.set_default_verify_paths(); ssl.set_verify_mode(asio::ssl::verify_peer);
            tcp::resolver resolver(io);
            websocket::stream<ssl_socket> ws(io, ssl);
            asio::connect(beast::get_lowest_layer(ws), resolver.resolve(host, port));
            if (!SSL_set_tlsext_host_name(ws.next_layer().native_handle(), host.c_str())) throw std::runtime_error("SNI failed");
            ws.next_layer().handshake(asio::ssl::stream_base::client);
            ws.handshake(host, path);
            ws.text(true);
            if (!subscription.empty()) ws.write(asio::buffer(subscription));
            set_connected(shared, source, true);
            std::cerr << "REFERENCE_CONNECTED source=" << source << "\n";
            beast::flat_buffer buffer;
            asio::steady_timer heartbeat(io);
            const std::string ping_message("PING");
            beast::error_code failure;
            bool finished = false;
            auto finish = [&](beast::error_code ec) {
                if (finished) return;
                finished = true;
                failure = ec;
                heartbeat.cancel();
                beast::get_lowest_layer(ws).cancel();
            };
            std::function<void()> read_next;
            read_next = [&] {
                ws.async_read(buffer, [&](beast::error_code ec, std::size_t) {
                    if (ec) return finish(ec);
                    const std::string raw = beast::buffers_to_string(buffer.data());
                    buffer.consume(buffer.size());
                    if (raw != "PONG") {
                        try { handler(raw); }
                        catch (...) {
                            std::lock_guard<std::mutex> lock(shared.mutex);
                            ++shared.unmatched_messages;
                        }
                    }
                    read_next();
                });
            };
            std::function<void()> schedule_heartbeat;
            schedule_heartbeat = [&] {
                heartbeat.expires_after(std::chrono::seconds(5));
                heartbeat.async_wait([&](beast::error_code ec) {
                    if (ec || finished) return;
                    ws.async_write(asio::buffer(ping_message), [&](beast::error_code write_ec, std::size_t) {
                        if (write_ec) return finish(write_ec);
                        schedule_heartbeat();
                    });
                });
            };
            read_next();
            if (text_ping) schedule_heartbeat();
            io.run();
            if (failure) throw boost::system::system_error(failure);
        } catch (const std::exception& error) {
            set_connected(shared, source, false);
            std::cerr << "REFERENCE_ERROR source=" << source << " message=" << error.what() << " reconnect_s=2\n";
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    }
}

ptree parse_json(const std::string& raw) {
    ptree row; std::istringstream input(raw); boost::property_tree::read_json(input, row); return row;
}

void initialize(SharedState& shared) {
    for (const auto& config : ASSETS) {
        auto& sources = shared.assets[config.asset].sources;
        sources["binance"] = {config.binance, "spot", "USDT", "", 0, 0, 0, 0, false, std::string(config.binance).size() > 0, {}, {}, {}};
        sources["coinbase"] = {config.coinbase, "spot", "USD", "", 0, 0, 0, 0, false, std::string(config.coinbase).size() > 0, {}, {}, {}};
        sources["kraken"] = {config.kraken, "spot", "USD", "", 0, 0, 0, 0, false, std::string(config.kraken).size() > 0, {}, {}, {}};
        sources["bybit"] = {config.bybit, "spot", "USDT", "", 0, 0, 0, 0, false, std::string(config.bybit).size() > 0, {}, {}, {}};
        sources["okx"] = {config.okx, "spot", "USDT", "", 0, 0, 0, 0, false, std::string(config.okx).size() > 0, {}, {}, {}};
        sources["chainlink"] = {config.chainlink, "settlement", "USD", "", 0, 0, 0, 0, false, std::string(config.chainlink).size() > 0, {}, {}, {}};
    }
}

int main(int argc, char** argv) {
    SharedState shared;
    shared.output_path = argc > 1 ? argv[1] : "data/venue-status.json";
    const std::string reference_socket_path = std::getenv("REFERENCE_IPC_PATH")
        ? std::getenv("REFERENCE_IPC_PATH") : "state/reference-price.sock";
    boost::asio::io_context reference_io;
    auto reference_publisher = std::make_shared<reference_ipc::LatestValueServer>(
        reference_io, reference_socket_path);
    reference_publisher->start();
    shared.reference_publisher = reference_publisher;
    shared.producer_session = std::to_string(
        std::chrono::steady_clock::now().time_since_epoch().count());
    std::thread reference_publisher_thread([&] { reference_io.run(); });
    initialize(shared);
    {
        std::lock_guard<std::mutex> lock(shared.mutex);
        publish_ipc_locked(shared, true);
        write_status_locked(shared, true);
    }

    const std::string rtds_sub = R"({"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":""}]})";
    const std::string binance_path = "/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/solusdt@bookTicker/xrpusdt@bookTicker/bnbusdt@bookTicker/dogeusdt@bookTicker/btcusdt@kline_1h/ethusdt@kline_1h/solusdt@kline_1h/xrpusdt@kline_1h/bnbusdt@kline_1h/dogeusdt@kline_1h/btcusdt@kline_4h/ethusdt@kline_4h/solusdt@kline_4h/xrpusdt@kline_4h/bnbusdt@kline_4h/dogeusdt@kline_4h";
    const std::string coinbase_sub = R"({"type":"subscribe","product_ids":["BTC-USD","ETH-USD","SOL-USD","XRP-USD","BNB-USD","DOGE-USD","HYPE-USD"],"channels":["ticker","level2_batch"]})";
    const std::string kraken_sub = R"({"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD","ETH/USD","SOL/USD","XRP/USD","BNB/USD","DOGE/USD","HYPE/USD"]}})";
    const std::string bybit_sub = R"({"op":"subscribe","args":["tickers.BTCUSDT","tickers.ETHUSDT","tickers.SOLUSDT","tickers.XRPUSDT","tickers.BNBUSDT","tickers.DOGEUSDT"]})";
    const std::string okx_sub = R"({"op":"subscribe","args":[{"channel":"tickers","instId":"BTC-USDT"},{"channel":"tickers","instId":"ETH-USDT"},{"channel":"tickers","instId":"SOL-USDT"},{"channel":"tickers","instId":"XRP-USDT"},{"channel":"tickers","instId":"BNB-USDT"},{"channel":"tickers","instId":"DOGE-USDT"},{"channel":"tickers","instId":"HYPE-USDT"}]})";

    std::thread binance([&] {
        websocket_loop(shared, "binance", "data-stream.binance.vision", "443", binance_path, "", false, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            const auto data = row.get_child_optional("data");
            if (!data) return;
            std::string symbol = data->get<std::string>("s", "");
            std::transform(symbol.begin(), symbol.end(), symbol.begin(), ::tolower);
            if (const auto kline = data->get_child_optional("k")) {
                const std::string timeframe = kline->get<std::string>("i", "");
                if (timeframe != "1h" && timeframe != "4h") return;
                const double open = kline->get<double>("o", 0);
                const double start = kline->get<double>("t", 0);
                if (!open || !start) return;
                for (const auto& config : ASSETS) if (symbol == config.binance) {
                    publish_anchor(shared, config.asset, "binance", open, start, timeframe);
                    if (kline->get<bool>("x", false)) {
                        const double close = kline->get<double>("c", 0);
                        const double duration = timeframe == "1h" ? 3600000 : 14400000;
                        if (close) publish_settlement(shared, config.asset, "binance", close, start + duration, timeframe);
                    }
                    return;
                }
                return;
            }
            const double bid = data->get<double>("b", 0);
            const double ask = data->get<double>("a", 0);
            if (!bid || !ask) return;
            for (const auto& config : ASSETS) if (symbol == config.binance)
                return publish(shared, config.asset, "binance", (bid + ask) / 2, bid, ask, "");
        });
    });
    std::thread rtds([&] {
        websocket_loop(shared, "chainlink", "ws-live-data.polymarket.com", "443", "/", rtds_sub, true, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            const std::string topic = row.get<std::string>("topic", "");
            std::string symbol = row.get<std::string>("payload.symbol", "");
            std::transform(symbol.begin(), symbol.end(), symbol.begin(), ::tolower);
            for (const auto& config : ASSETS) {
                if (topic == "crypto_prices_chainlink" && symbol == config.chainlink)
                    return publish(shared, config.asset, "chainlink", row.get<double>("payload.value"), 0, 0, row.get<std::string>("payload.timestamp", ""));
            }
            std::lock_guard<std::mutex> lock(shared.mutex);
            ++shared.unmatched_messages;
            if (shared.unmatched_messages <= 20) {
                std::cerr << "REFERENCE_UNMATCHED source=chainlink topic=" << topic
                          << " type=" << row.get<std::string>("type", "")
                          << " symbol=" << symbol << " raw=" << raw.substr(0, 500) << "\n";
            }
        });
    });
    std::thread coinbase([&] {
        std::map<std::string, CoinbaseBook> books;
        websocket_loop(shared, "coinbase", "ws-feed.exchange.coinbase.com", "443", "/", coinbase_sub, false, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            const std::string type = row.get<std::string>("type", "");
            std::string symbol = row.get<std::string>("product_id", "");
            double bid = 0, ask = 0, price = 0;
            if (type == "ticker") {
                price = row.get<double>("price", 0);
                bid = row.get<double>("best_bid", 0);
                ask = row.get<double>("best_ask", 0);
            } else if (apply_coinbase_book_message(row, books, symbol, bid, ask)) {
                price = (bid + ask) / 2;
            }
            if (!price) return;
            for (const auto& config : ASSETS) if (symbol == config.coinbase)
                return publish(shared, config.asset, "coinbase", price, bid, ask, row.get<std::string>("time", ""));
        });
    });
    std::thread kraken([&] {
        websocket_loop(shared, "kraken", "ws.kraken.com", "443", "/v2", kraken_sub, false, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            if (row.get<std::string>("channel", "") != "ticker") return;
            const auto data = row.get_child_optional("data");
            if (!data || data->empty()) return;
            const auto& tick = data->front().second;
            const std::string symbol = tick.get<std::string>("symbol", "");
            for (const auto& config : ASSETS) if (symbol == config.kraken)
                return publish(shared, config.asset, "kraken", tick.get<double>("last"), tick.get<double>("bid", 0), tick.get<double>("ask", 0), tick.get<std::string>("timestamp", ""));
        });
    });
    std::thread bybit([&] {
        websocket_loop(shared, "bybit", "stream.bybit.com", "443", "/v5/public/spot", bybit_sub, false, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            if (row.get<std::string>("topic", "").rfind("tickers.", 0) != 0) return;
            const auto data = row.get_child_optional("data");
            if (!data) return;
            const std::string symbol = data->get<std::string>("symbol", "");
            const double bid = data->get<double>("bid1Price", 0);
            const double ask = data->get<double>("ask1Price", 0);
            const double last = data->get<double>("lastPrice", 0);
            const double price = bid > 0 && ask > 0 ? (bid + ask) / 2 : last;
            if (!price) return;
            for (const auto& config : ASSETS) if (symbol == config.bybit)
                return publish(shared, config.asset, "bybit", price, bid, ask, row.get<std::string>("ts", ""));
        });
    });
    std::thread okx([&] {
        websocket_loop(shared, "okx", "ws.okx.com", "8443", "/ws/v5/public", okx_sub, false, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            if (row.get<std::string>("arg.channel", "") != "tickers") return;
            const auto data = row.get_child_optional("data");
            if (!data || data->empty()) return;
            const auto& tick = data->front().second;
            const std::string symbol = tick.get<std::string>("instId", "");
            const double bid = tick.get<double>("bidPx", 0), ask = tick.get<double>("askPx", 0);
            if (!bid || !ask) return;
            for (const auto& config : ASSETS) if (symbol == config.okx)
                return publish(shared, config.asset, "okx", (bid + ask) / 2, bid, ask, tick.get<std::string>("ts", ""));
        });
    });
    binance.join(); rtds.join(); coinbase.join(); kraken.join(); bybit.join(); okx.join();
    reference_io.stop();
    reference_publisher_thread.join();
}
