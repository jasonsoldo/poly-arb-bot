#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>
#include <chrono>
#include <algorithm>
#include <cctype>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <iomanip>
#include <map>
#include <sstream>
#include <string>
#include <thread>

namespace asio = boost::asio;
namespace beast = boost::beast;
namespace websocket = beast::websocket;
using tcp = asio::ip::tcp;
using ssl_socket = asio::ssl::stream<tcp::socket>;
using boost::property_tree::ptree;

double now_ms() {
    return std::chrono::duration<double, std::milli>(std::chrono::system_clock::now().time_since_epoch()).count();
}

struct PriceState {
    double binance = 0, chainlink = 0;
    double binance_source_ms = 0, chainlink_source_ms = 0;
    double engine_latency_us = 0;
};

struct AssetConfig {
    const char* asset;
    const char* binance;
    const char* chainlink;
    bool supported;
};

const char* source_status(double source_ms, double timestamp) {
    if (!source_ms) return "NOT_RECEIVED";
    return timestamp - source_ms <= 10000 ? "FRESH" : "STALE";
}

const AssetConfig ASSETS[] = {
    {"BTC", "btcusdt", "btc/usd", true}, {"ETH", "ethusdt", "eth/usd", true},
    {"SOL", "solusdt", "sol/usd", true}, {"XRP", "xrpusdt", "xrp/usd", true},
    {"BNB", "bnbusdt", "bnb/usd", false}, {"DOGE", "dogeusdt", "doge/usd", false},
    {"HYPE", "hypeusdt", "hype/usd", false},
};

void write_status(const std::string& path, const std::map<std::string, PriceState>& states, double engine_latency_us,
                  unsigned long long matched_messages, unsigned long long unmatched_messages) {
    const std::string temporary = path + ".tmp";
    std::ofstream out(temporary, std::ios::trunc);
    out << std::setprecision(15);
    const double timestamp = now_ms();
    out << "{\"updated_at_ms\":" << timestamp;
    out << ",\"assets\":{";
    bool first = true;
    for (const auto& config : ASSETS) {
        if (!first) out << ',';
        first = false;
        const auto& state = states.at(config.asset);
        out << '\"' << config.asset << "\":{\"supported\":" << (config.supported ? "true" : "false");
        if (state.binance) out << ",\"binance\":" << state.binance; else out << ",\"binance\":null";
        if (state.chainlink) out << ",\"chainlink\":" << state.chainlink; else out << ",\"chainlink\":null";
        out << ",\"binance_status\":\"" << (config.supported ? source_status(state.binance_source_ms, timestamp) : "UNSUPPORTED")
            << "\",\"chainlink_status\":\"" << (config.supported ? source_status(state.chainlink_source_ms, timestamp) : "UNSUPPORTED") << '"';
        if (state.binance && state.chainlink) {
            out << ",\"divergence_bps\":" << (state.binance - state.chainlink) / state.chainlink * 10000;
        } else out << ",\"divergence_bps\":null";
        out << ",\"binance_source_age_ms\":" << (state.binance_source_ms ? timestamp - state.binance_source_ms : -1)
            << ",\"chainlink_source_age_ms\":" << (state.chainlink_source_ms ? timestamp - state.chainlink_source_ms : -1) << '}';
    }
    out << "},\"engine_latency_us\":" << engine_latency_us
        << ",\"matched_messages\":" << matched_messages << ",\"unmatched_messages\":" << unmatched_messages << "}\n";
    out.flush();
    out.close();
    std::filesystem::rename(temporary, path);
}

void run(const std::string& output_path) {
    const std::string host = "ws-live-data.polymarket.com";
    asio::io_context io;
    asio::ssl::context ssl(asio::ssl::context::tls_client);
    ssl.set_default_verify_paths(); ssl.set_verify_mode(asio::ssl::verify_peer);
    tcp::resolver resolver(io);
    websocket::stream<ssl_socket> ws(io, ssl);
    const auto endpoints = resolver.resolve(host, "443");
    asio::connect(beast::get_lowest_layer(ws), endpoints);
    if (!SSL_set_tlsext_host_name(ws.next_layer().native_handle(), host.c_str())) throw std::runtime_error("SNI failed");
    ws.next_layer().handshake(asio::ssl::stream_base::client);
    ws.handshake(host, "/");
    const std::string subscription = R"({"action":"subscribe","subscriptions":[{"topic":"crypto_prices","type":"update","filters":"btcusdt,ethusdt,solusdt,xrpusdt"},{"topic":"crypto_prices_chainlink","type":"*","filters":""}]})";
    ws.write(asio::buffer(subscription));
    std::cerr << "REFERENCE_CONNECTED sources=binance,chainlink\n";
    std::map<std::string, PriceState> states;
    for (const auto& config : ASSETS) states.emplace(config.asset, PriceState{});
    unsigned long long matched_messages = 0, unmatched_messages = 0;
    write_status(output_path, states, 0, matched_messages, unmatched_messages);
    double last_ping = now_ms();
    for (;;) {
        beast::flat_buffer buffer;
        ws.read(buffer);
        const double received_ms = now_ms();
        const std::string raw = beast::buffers_to_string(buffer.data());
        if (raw == "PONG") continue;
        ptree message;
        try { std::istringstream input(raw); boost::property_tree::read_json(input, message); }
        catch (...) { continue; }
        const std::string topic = message.get<std::string>("topic", "");
        const double value = message.get<double>("payload.value", 0);
        const double source_ms = message.get<double>("payload.timestamp", message.get<double>("timestamp", 0));
        std::string symbol = message.get<std::string>("payload.symbol", "");
        std::transform(symbol.begin(), symbol.end(), symbol.begin(), [](unsigned char c) { return std::tolower(c); });
        bool matched = false;
        for (const auto& config : ASSETS) {
            auto& state = states.at(config.asset);
            if (topic == "crypto_prices" && symbol == config.binance && config.supported) {
                state.binance = value; state.binance_source_ms = source_ms; matched = true; break;
            }
            if (topic == "crypto_prices_chainlink" && symbol == config.chainlink && config.supported) {
                state.chainlink = value; state.chainlink_source_ms = source_ms; matched = true; break;
            }
        }
        if (matched) ++matched_messages; else ++unmatched_messages;
        write_status(output_path, states, (now_ms() - received_ms) * 1000, matched_messages, unmatched_messages);
        if (now_ms() - last_ping >= 5000) { ws.write(asio::buffer(std::string("PING"))); last_ping = now_ms(); }
    }
}

int main(int argc, char** argv) {
    const std::string output = argc > 1 ? argv[1] : "data/venue-status.json";
    for (;;) {
        try { run(output); }
        catch (const std::exception& error) { std::cerr << "REFERENCE_ERROR message=" << error.what() << " reconnect_s=2\n"; }
        std::this_thread::sleep_for(std::chrono::seconds(2));
    }
}
