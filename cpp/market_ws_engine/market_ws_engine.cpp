#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>
#include <algorithm>
#include <chrono>
#include <deque>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
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

struct Book { std::map<double, double> bids, asks; };
struct Market {
    std::string up, down, last_reason;
    double size = 10, fee = .07, active_since = 0, last_audit = 0;
};

double now_seconds() { return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count(); }
double number(const ptree& row, const std::string& key) { return row.get<double>(key, 0); }

void set_levels(Book& book, const ptree& rows, bool bid, bool clear) {
    auto& side = bid ? book.bids : book.asks;
    if (clear) side.clear();
    for (const auto& item : rows) {
        const auto& row = item.second;
        const double price = number(row, "price"), size = number(row, "size");
        if (size <= 0) side.erase(price); else side[price] = size;
    }
}

void update_level(Book& book, const ptree& row) {
    auto& side = row.get<std::string>("side", "") == "BUY" ? book.bids : book.asks;
    const double price = number(row, "price"), size = number(row, "size");
    if (size <= 0) side.erase(price); else side[price] = size;
}

std::pair<double, double> buy_vwap(const Book& book, double size) {
    double left = size, filled = 0, notional = 0;
    for (const auto& level : book.asks) {
        const double take = std::min(left, level.second);
        filled += take; notional += take * level.first; left -= take;
        if (left <= 1e-9) break;
    }
    return {filled, filled ? notional / filled : 0};
}

class MarketWsSession : public std::enable_shared_from_this<MarketWsSession> {
public:
    MarketWsSession(asio::io_context& io, asio::ssl::context& ssl, std::map<std::string, Market> markets, double size, double fee)
        : resolver_(io), ws_(io, ssl), timer_(io), markets_(std::move(markets)), size_(size), fee_(fee), last_activity_(now_seconds()) {
        for (const auto& item : markets_) { books_[item.second.up]; books_[item.second.down]; }
    }

    void run() {
        resolver_.async_resolve(host_, "443", beast::bind_front_handler(&MarketWsSession::on_resolve, shared_from_this()));
    }

private:
    void on_resolve(beast::error_code ec, tcp::resolver::results_type results) {
        if (ec) return fail("resolve", ec);
        asio::async_connect(beast::get_lowest_layer(ws_), results, beast::bind_front_handler(&MarketWsSession::on_connect, shared_from_this()));
    }

    void on_connect(beast::error_code ec, const tcp::resolver::results_type::endpoint_type&) {
        if (ec) return fail("connect", ec);
        if (!SSL_set_tlsext_host_name(ws_.next_layer().native_handle(), host_.c_str())) return fail("sni", beast::error_code(static_cast<int>(::ERR_get_error()), asio::error::get_ssl_category()));
        ws_.next_layer().async_handshake(asio::ssl::stream_base::client, beast::bind_front_handler(&MarketWsSession::on_tls, shared_from_this()));
    }

    void on_tls(beast::error_code ec) {
        if (ec) return fail("tls", ec);
        ws_.set_option(websocket::stream_base::timeout::suggested(beast::role_type::client));
        ws_.async_handshake(host_, "/ws/market", beast::bind_front_handler(&MarketWsSession::on_handshake, shared_from_this()));
    }

    void on_handshake(beast::error_code ec) {
        if (ec) return fail("websocket_handshake", ec);
        std::vector<std::string> assets;
        for (const auto& item : books_) assets.push_back(item.first);
        const size_t first_count = std::min<size_t>(20, assets.size());
        queue_write(subscription(std::vector<std::string>(assets.begin(), assets.begin() + first_count), ""));
        for (size_t offset = first_count; offset < assets.size(); offset += 20) {
            const size_t end = std::min(offset + 20, assets.size());
            queue_write(subscription(std::vector<std::string>(assets.begin() + offset, assets.begin() + end), "subscribe"));
        }
        schedule_ping();
        do_read();
        std::cout << "connected tokens=" << assets.size() << "\n" << std::flush;
    }

    void do_read() { ws_.async_read(buffer_, beast::bind_front_handler(&MarketWsSession::on_read, shared_from_this())); }

    void on_read(beast::error_code ec, std::size_t) {
        if (ec == websocket::error::closed) return fail("closed", ec);
        if (ec) return fail("read", ec);
        const std::string raw = beast::buffers_to_string(buffer_.data());
        buffer_.consume(buffer_.size());
        last_activity_ = now_seconds();
        if (raw == "PONG") { std::cerr << "WS_PONG\n"; do_read(); return; }
        handle_message(raw);
        do_read();
    }

    void handle_message(const std::string& raw) {
        ptree message;
        try { std::istringstream input(raw); boost::property_tree::read_json(input, message); }
        catch (...) { std::cerr << "WS_NON_JSON " << raw << "\n"; return; }
        const std::string type = message.get<std::string>("event_type", "");
        const std::string asset = message.get<std::string>("asset_id", "");
        if (type == "book" && books_.count(asset)) {
            if (auto bids = message.get_child_optional("bids")) set_levels(books_[asset], *bids, true, true);
            if (auto asks = message.get_child_optional("asks")) set_levels(books_[asset], *asks, false, true);
            ++book_events_;
        } else if (type == "price_change") {
            auto changes = message.get_child_optional("price_changes");
            if (!changes) return;
            for (const auto& item : *changes) {
                const auto& row = item.second; const std::string token = row.get<std::string>("asset_id", asset);
                if (!books_.count(token)) continue;
                update_level(books_[token], row);
            }
            ++price_changes_;
        }
        if (type == "book" || (type == "price_change" && price_changes_ % 100 == 0))
            std::cerr << "WS_DATA type=" << type << " books=" << book_events_ << " changes=" << price_changes_ << "\n";
        evaluate();
    }

    void evaluate() {
        for (auto& item : markets_) {
            auto up = buy_vwap(books_[item.second.up], size_), down = buy_vwap(books_[item.second.down], size_);
            const bool fok = up.first >= size_ && down.first >= size_;
            const double up_fee = up.first * fee_ * up.second * (1 - up.second), down_fee = down.first * fee_ * down.second * (1 - down.second);
            const double total = size_ * (up.second + down.second) + up_fee + down_fee, profit = fok ? size_ - total : 0;
            const bool good = fok && profit > 0; const double timestamp = now_seconds();
            const std::string reason = up.first < size_ ? "up_depth" : down.first < size_ ? "down_depth" : profit <= 0 ? "no_edge" : "opportunity";
            if (good && item.second.active_since == 0) item.second.active_since = timestamp;
            if (!good) item.second.active_since = 0;
            if (reason != item.second.last_reason || timestamp - item.second.last_audit >= 5) {
                std::cout << "SHADOW_EVAL\tmarket=" << item.first << "\treason=" << reason
                          << "\tfok=" << (fok ? 1 : 0) << "\tup_fill=" << up.first << "\tdown_fill=" << down.first
                          << "\tup_vwap=" << up.second << "\tdown_vwap=" << down.second
                          << "\tfees=" << up_fee + down_fee << "\ttotal=" << total << "\tprofit=" << profit << "\n" << std::flush;
                item.second.last_reason = reason;
                item.second.last_audit = timestamp;
            }
            if (good) std::cout << "SHADOW_OPPORTUNITY\tmarket=" << item.first << "\tup_vwap=" << std::setprecision(12) << up.second
                                << "\tdown_vwap=" << down.second << "\tfees=" << up_fee + down_fee << "\ttotal=" << total
                                << "\tprofit=" << profit << "\tfok=1\tduration_ms=" << (timestamp - item.second.active_since) * 1000 << "\n" << std::flush;
        }
    }

    void schedule_ping() {
        timer_.expires_after(std::chrono::seconds(5));
        timer_.async_wait([self = shared_from_this()](beast::error_code ec) {
            if (ec || self->stopped_) return;
            self->queue_write("PING");
            const double idle = now_seconds() - self->last_activity_;
            if (idle > 30) std::cerr << "WS_STALE idle_s=" << idle << "\n";
            self->schedule_ping();
        });
    }

    void queue_write(std::string message) {
        if (stopped_) return;
        const bool idle = writes_.empty(); writes_.push_back(std::move(message));
        if (idle) do_write();
    }

    void do_write() {
        ws_.text(true);
        ws_.async_write(asio::buffer(writes_.front()), beast::bind_front_handler(&MarketWsSession::on_write, shared_from_this()));
    }

    void on_write(beast::error_code ec, std::size_t) {
        if (ec) return fail("write", ec);
        const std::string sent = writes_.front(); writes_.pop_front();
        if (sent == "PING") std::cerr << "WS_PING\n"; else std::cerr << "WS_SUBSCRIBE tokens_message_sent\n";
        if (!writes_.empty()) do_write();
    }

    static std::string subscription(const std::vector<std::string>& assets, const std::string& operation) {
        std::string message = "{\"assets_ids\":[";
        for (size_t i = 0; i < assets.size(); ++i) { if (i) message += ','; message += "\"" + assets[i] + "\""; }
        message += operation.empty() ? "],\"type\":\"market\",\"custom_feature_enabled\":true}" : "],\"operation\":\"subscribe\"}";
        return message;
    }

    void fail(const char* stage, beast::error_code ec) {
        if (stopped_) return;
        stopped_ = true; timer_.cancel();
        std::cerr << "WS_ERROR stage=" << stage << " code=" << ec.value() << " message=" << ec.message() << "\n";
    }

    const std::string host_ = "ws-subscriptions-clob.polymarket.com";
    tcp::resolver resolver_;
    websocket::stream<ssl_socket> ws_;
    beast::flat_buffer buffer_;
    asio::steady_timer timer_;
    std::deque<std::string> writes_;
    std::map<std::string, Market> markets_;
    std::map<std::string, Book> books_;
    double size_, fee_, last_activity_;
    unsigned long long book_events_ = 0, price_changes_ = 0;
    bool stopped_ = false;
};

int main(int argc, char** argv) {
    if (argc < 2) { std::cerr << "usage: market_ws_engine <markets.json> [size] [fee_rate]\n"; return 2; }
    try {
        std::ifstream file(argv[1]); ptree root; boost::property_tree::read_json(file, root);
        std::map<std::string, Market> markets;
        for (const auto& item : root.get_child("markets")) {
            const auto& row = item.second;
            markets[row.get<std::string>("market_id")] = {row.get<std::string>("up_token_id"), row.get<std::string>("down_token_id")};
        }
        if (markets.empty()) { std::cerr << "NO_TOKENS live_markets.json contains no valid Up/Down tokens\n"; return 4; }
        const double size = argc > 2 ? std::stod(argv[2]) : 10, fee = argc > 3 ? std::stod(argv[3]) : .07;
        for (;;) {
            asio::io_context io; asio::ssl::context ssl(asio::ssl::context::tls_client);
            ssl.set_default_verify_paths(); ssl.set_verify_mode(asio::ssl::verify_peer);
            auto session = std::make_shared<MarketWsSession>(io, ssl, markets, size, fee);
            session->run(); io.run();
            std::cerr << "WS_RECONNECT delay_s=2\n";
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    } catch (const std::exception& error) { std::cerr << "FATAL " << error.what() << "\n"; return 1; }
}
