#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>
#include <algorithm>
#include <chrono>
#include <cmath>
#include <deque>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
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

struct AssetState { std::map<std::string, SourceState> sources; };

struct AssetConfig {
    const char* asset;
    const char* binance;
    const char* chainlink;
    const char* coinbase;
    const char* kraken;
};

const AssetConfig ASSETS[] = {
    {"BTC", "btcusdt", "btc/usd", "BTC-USD", "BTC/USD"},
    {"ETH", "ethusdt", "eth/usd", "ETH-USD", "ETH/USD"},
    {"SOL", "solusdt", "sol/usd", "SOL-USD", "SOL/USD"},
    {"XRP", "xrpusdt", "xrp/usd", "XRP-USD", "XRP/USD"},
    {"BNB", "bnbusdt", "bnb/usd", "", ""},
    {"DOGE", "dogeusdt", "doge/usd", "DOGE-USD", "DOGE/USD"},
    {"HYPE", "", "hype/usd", "", ""},
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
};

constexpr double STATUS_WRITE_INTERVAL_MS = 100;

std::string source_status(const SourceState& source, double timestamp) {
    if (!source.supported) return "UNSUPPORTED";
    if (!source.connected) return source.received_at ? "DISCONNECTED" : "NOT_RECEIVED";
    if (!source.received_at) return "NOT_RECEIVED";
    return timestamp - source.received_at <= 10000 ? "FRESH" : "STALE";
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

double momentum_bps(const SourceState& source, double timestamp, double horizon_ms = 30000) {
    if (source.samples.size() < 2 || source.samples.back().second <= 0) return 0;
    auto start = source.samples.begin();
    while (start != source.samples.end() && timestamp - start->first > horizon_ms) ++start;
    if (start == source.samples.end() || start->second <= 0) return 0;
    return (source.samples.back().second / start->second - 1) * 10000;
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
        double fast_price = 0, settlement_reference = 0;
        int fresh_sources = 0, fresh_usd_sources = 0;
        std::vector<double> volatilities, momentums;
        int model_sample_count = 0;
        for (const auto& item : asset.sources) {
            const auto& source = item.second;
            if (source_status(source, timestamp) != "FRESH" || source.market_type != "spot" || !source.price) continue;
            if (item.first != "chainlink") {
                fresh_spot.push_back(source.price);
                ++fresh_sources;
                if (source.quote_currency == "USD") { fresh_usd.push_back(source.price); ++fresh_usd_sources; }
                if (!fast_price && (item.first == "binance" || item.first == "coinbase" || item.first == "kraken")) fast_price = source.price;
                const double volatility = volatility_per_sqrt_second(source);
                if (volatility > 0) volatilities.push_back(volatility);
                if (source.samples.size() >= 2) momentums.push_back(momentum_bps(source, timestamp));
                model_sample_count = std::max(model_sample_count, static_cast<int>(source.samples.size()));
            } else {
                settlement_reference = source.price;
            }
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
            out << ",\"status\":\"" << source_status(source, timestamp) << "\"}";
        }
        const auto& binance = asset.sources.at("binance");
        const auto& chainlink = asset.sources.at("chainlink");
        out << "},\"binance\":"; write_number(out, binance.price);
        out << ",\"chainlink\":"; write_number(out, chainlink.price);
        out << ",\"binance_status\":\"" << source_status(binance, timestamp)
            << "\",\"chainlink_status\":\"" << source_status(chainlink, timestamp) << "\""
            << ",\"binance_source_age_ms\":";
        if (binance.received_at) out << std::max(0.0, timestamp - binance.received_at); else out << -1;
        out << ",\"chainlink_source_age_ms\":";
        if (chainlink.received_at) out << std::max(0.0, timestamp - chainlink.received_at); else out << -1;
        out << ",\"fresh_exchange_source_count\":" << fresh_sources
            << ",\"fresh_usd_spot_source_count\":" << fresh_usd_sources
            << ",\"consensus_price\":"; write_number(out, consensus);
        out << ",\"fast_price\":"; write_number(out, fast_price);
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
        out << ",\"model_sample_count\":" << model_sample_count << '}';
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
    row.price = price; row.bid = bid; row.ask = ask;
    row.source_timestamp = source_timestamp; row.received_at = received; row.connected = true;
    row.samples.emplace_back(received, price);
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
    write_status_locked(shared);
}

void publish_anchor(SharedState& shared, const std::string& asset, const std::string& source,
                    double price, double source_timestamp_ms, const std::string& timeframe) {
    const double received = now_ms();
    std::lock_guard<std::mutex> lock(shared.mutex);
    auto& samples = shared.assets.at(asset).sources.at(source).anchor_samples;
    samples.push_back({source_timestamp_ms, received, price, timeframe});
    while (samples.size() > 128) samples.pop_front();
    write_status_locked(shared);
}

void publish_settlement(SharedState& shared, const std::string& asset, const std::string& source,
                        double price, double source_timestamp_ms, const std::string& timeframe) {
    const double received = now_ms();
    std::lock_guard<std::mutex> lock(shared.mutex);
    auto& samples = shared.assets.at(asset).sources.at(source).settlement_samples;
    samples.push_back({source_timestamp_ms, received, price, timeframe});
    while (samples.size() > 128) samples.pop_front();
    write_status_locked(shared, true);
}

void set_connected(SharedState& shared, const std::string& source, bool connected) {
    std::lock_guard<std::mutex> lock(shared.mutex);
    for (auto& asset : shared.assets) {
        auto& row = asset.second.sources.at(source);
        if (row.supported) row.connected = connected;
    }
    write_status_locked(shared, true);
}

template <typename Handler>
void websocket_loop(SharedState& shared, const std::string& source, const std::string& host,
                    const std::string& path, const std::string& subscription,
                    bool text_ping, Handler handler) {
    for (;;) {
        try {
            asio::io_context io;
            asio::ssl::context ssl(asio::ssl::context::tls_client);
            ssl.set_default_verify_paths(); ssl.set_verify_mode(asio::ssl::verify_peer);
            tcp::resolver resolver(io);
            websocket::stream<ssl_socket> ws(io, ssl);
            asio::connect(beast::get_lowest_layer(ws), resolver.resolve(host, "443"));
            if (!SSL_set_tlsext_host_name(ws.next_layer().native_handle(), host.c_str())) throw std::runtime_error("SNI failed");
            ws.next_layer().handshake(asio::ssl::stream_base::client);
            ws.handshake(host, path);
            if (!subscription.empty()) ws.write(asio::buffer(subscription));
            set_connected(shared, source, true);
            std::cerr << "REFERENCE_CONNECTED source=" << source << "\n";
            double last_ping = now_ms();
            for (;;) {
                beast::flat_buffer buffer;
                ws.read(buffer);
                const std::string raw = beast::buffers_to_string(buffer.data());
                if (raw == "PONG") continue;
                try { handler(raw); }
                catch (...) { std::lock_guard<std::mutex> lock(shared.mutex); ++shared.unmatched_messages; }
                if (text_ping && now_ms() - last_ping >= 5000) {
                    ws.write(asio::buffer(std::string("PING")));
                    last_ping = now_ms();
                }
            }
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
        sources["chainlink"] = {config.chainlink, "settlement", "USD", "", 0, 0, 0, 0, false, std::string(config.chainlink).size() > 0, {}, {}, {}};
    }
}

int main(int argc, char** argv) {
    SharedState shared;
    shared.output_path = argc > 1 ? argv[1] : "data/venue-status.json";
    initialize(shared);
    { std::lock_guard<std::mutex> lock(shared.mutex); write_status_locked(shared, true); }

    const std::string rtds_sub = R"({"action":"subscribe","subscriptions":[{"topic":"crypto_prices_chainlink","type":"*","filters":""}]})";
    const std::string binance_path = "/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker/solusdt@bookTicker/xrpusdt@bookTicker/bnbusdt@bookTicker/dogeusdt@bookTicker/btcusdt@kline_1h/ethusdt@kline_1h/solusdt@kline_1h/xrpusdt@kline_1h/bnbusdt@kline_1h/dogeusdt@kline_1h/btcusdt@kline_4h/ethusdt@kline_4h/solusdt@kline_4h/xrpusdt@kline_4h/bnbusdt@kline_4h/dogeusdt@kline_4h";
    const std::string coinbase_sub = R"({"type":"subscribe","product_ids":["BTC-USD","ETH-USD","SOL-USD","XRP-USD","DOGE-USD"],"channels":["ticker"]})";
    const std::string kraken_sub = R"({"method":"subscribe","params":{"channel":"ticker","symbol":["BTC/USD","ETH/USD","SOL/USD","XRP/USD","DOGE/USD"]}})";

    std::thread binance([&] {
        websocket_loop(shared, "binance", "data-stream.binance.vision", binance_path, "", false, [&](const std::string& raw) {
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
        websocket_loop(shared, "chainlink", "ws-live-data.polymarket.com", "/", rtds_sub, true, [&](const std::string& raw) {
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
        websocket_loop(shared, "coinbase", "ws-feed.exchange.coinbase.com", "/", coinbase_sub, false, [&](const std::string& raw) {
            const auto row = parse_json(raw);
            if (row.get<std::string>("type", "") != "ticker") return;
            const std::string symbol = row.get<std::string>("product_id", "");
            for (const auto& config : ASSETS) if (symbol == config.coinbase)
                return publish(shared, config.asset, "coinbase", row.get<double>("price"), row.get<double>("best_bid", 0), row.get<double>("best_ask", 0), row.get<std::string>("time", ""));
        });
    });
    std::thread kraken([&] {
        websocket_loop(shared, "kraken", "ws.kraken.com", "/v2", kraken_sub, false, [&](const std::string& raw) {
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
    binance.join(); rtds.join(); coinbase.join(); kraken.join();
}
