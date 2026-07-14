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
#include <atomic>
#include <chrono>
#include <cmath>
#include <deque>
#include <fstream>
#include <filesystem>
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

struct Book {
    std::map<double, double> bids, asks;
    bool initialized = false, ws_snapshot = false;
    double updated_at = 0, source_timestamp_ms = 0;
    std::string hash;
    unsigned long long generation = 0;
};
struct Market {
    std::string up, down, last_reason;
    double fee = .07, close_ts = 0, active_since = 0, last_audit = 0;
    Market() = default;
    Market(const std::string& up_token, const std::string& down_token) : up(up_token), down(down_token) {}
};

double now_seconds();

std::map<std::string, Market> load_markets(const std::string& path, unsigned long long* version_out = nullptr, double* generated_at_out = nullptr) {
    std::ifstream file(path); ptree root; boost::property_tree::read_json(file, root);
    const auto version = root.get<unsigned long long>("version", 0);
    const double generated_at = root.get<double>("generated_at", 0);
    if (!version || generated_at <= 0) throw std::runtime_error("market document metadata missing");
    if (generated_at > now_seconds() + 300) throw std::runtime_error("market document from future");
    std::map<std::string, Market> markets;
    std::map<std::string, bool> tokens;
    for (const auto& item : root.get_child("markets")) {
        const auto& row = item.second;
        Market market(row.get<std::string>("up_token_id"), row.get<std::string>("down_token_id"));
        market.fee = row.get<double>("fee_rate");
        market.close_ts = row.get<double>("close_ts", 0);
        if (market.close_ts <= now_seconds()) continue;
        if (market.up == market.down || tokens.count(market.up) || tokens.count(market.down)) throw std::runtime_error("duplicate market token");
        if (market.fee <= 0) throw std::runtime_error("invalid market fee");
        tokens[market.up] = true; tokens[market.down] = true;
        markets[row.get<std::string>("market_id")] = market;
    }
    if (markets.empty()) throw std::runtime_error("no unexpired markets");
    if (markets.size() > 56) throw std::runtime_error("market count exceeds configured asset/timeframe limit");
    if (version_out) *version_out = version;
    if (generated_at_out) *generated_at_out = generated_at;
    return markets;
}

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

bool update_level(Book& book, const ptree& row) {
    auto& side = row.get<std::string>("side", "") == "BUY" ? book.bids : book.asks;
    const double price = number(row, "price"), size = number(row, "size");
    if (size < 0) return false;
    if (size == 0 && !side.count(price)) return false;
    if (size == 0) side.erase(price); else side[price] = size;
    return true;
}

bool crossed(const Book& book) {
    return !book.bids.empty() && !book.asks.empty() && book.bids.rbegin()->first >= book.asks.begin()->first;
}

double best_ask(const Book& book) { return book.asks.empty() ? 0 : book.asks.begin()->first; }

double book_imbalance(const Book& book) {
    double bids = 0, asks = 0;
    for (const auto& level : book.bids) bids += level.second;
    for (const auto& level : book.asks) asks += level.second;
    return bids + asks > 0 ? (bids - asks) / (bids + asks) : 0;
}

double available_ask_depth(const Book& book) {
    double depth = 0;
    for (const auto& level : book.asks) depth += level.second;
    return depth;
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
    MarketWsSession(asio::io_context& io, asio::ssl::context& ssl, std::map<std::string, Market> markets,
                    std::map<std::string, Book> books, double size, double fee, double buffer_per_share,
                    double min_profit, double leg_interval_us, double execution_half_life_us,
                    double orphan_loss_per_share, double min_expected_value, const std::string& audit_path,
                    const std::string& markets_path, unsigned long long document_version, const std::string& health_path)
        : io_(io), ssl_(ssl), resolver_(io), ws_(io, ssl), timer_(io), reload_timer_(io), markets_(std::move(markets)), books_(std::move(books)),
          size_(size), fallback_fee_(fee), buffer_per_share_(buffer_per_share), min_profit_(min_profit),
          leg_interval_us_(leg_interval_us), execution_half_life_us_(execution_half_life_us),
          orphan_loss_per_share_(orphan_loss_per_share), min_expected_value_(min_expected_value),
          last_activity_(now_seconds()), audit_(audit_path, std::ios::app), markets_path_(markets_path), health_path_(health_path),
          run_id_(std::to_string(static_cast<unsigned long long>(now_seconds() * 1000000))),
          document_version_(document_version), generation_(1), ws_session_id_(++next_session_id_) {
        audit_ << std::setprecision(15);
        for (auto& item : books_) item.second.generation = generation_;
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
        schedule_reload();
        do_read();
        std::cout << "connected tokens=" << assets.size() << "\n" << std::flush;
        write_health(true);
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
        if (!message.get_optional<std::string>("event_type")) {
            for (const auto& item : message) process_message(item.second);
        } else {
            process_message(message);
        }
        evaluate();
    }

    void process_message(const ptree& message) {
        const std::string type = message.get<std::string>("event_type", "");
        const std::string asset = message.get<std::string>("asset_id", "");
        if (type == "book" && books_.count(asset)) {
            if (auto bids = message.get_child_optional("bids")) set_levels(books_[asset], *bids, true, true);
            if (auto asks = message.get_child_optional("asks")) set_levels(books_[asset], *asks, false, true);
            books_[asset].initialized = true;
            books_[asset].ws_snapshot = true;
            books_[asset].updated_at = now_seconds();
            books_[asset].source_timestamp_ms = message.get<double>("timestamp", 0);
            books_[asset].hash = message.get<std::string>("hash", "");
            ++book_events_;
        } else if (type == "price_change") {
            const double source_timestamp = message.get<double>("timestamp", 0);
            auto changes = message.get_child_optional("price_changes");
            if (!changes) return;
            for (const auto& item : *changes) {
                const auto& row = item.second; const std::string token = row.get<std::string>("asset_id", asset);
                if (!books_.count(token) || books_[token].generation != generation_) continue;
                if (!books_[token].ws_snapshot) continue;
                if (source_timestamp && books_[token].source_timestamp_ms && source_timestamp < books_[token].source_timestamp_ms) {
                    resync_token(token, "timestamp_rollback");
                    continue;
                }
                if (!update_level(books_[token], row)) {
                    resync_token(token, "invalid_level_update");
                    continue;
                }
                books_[token].updated_at = now_seconds();
                books_[token].source_timestamp_ms = source_timestamp;
                books_[token].hash = row.get<std::string>("hash", books_[token].hash);
                if (crossed(books_[token])) resync_token(token, "crossed_book");
            }
            ++price_changes_;
        }
        if (type == "book" || (type == "price_change" && price_changes_ % 100 == 0))
            std::cerr << "WS_DATA type=" << type << " books=" << book_events_ << " changes=" << price_changes_ << "\n";
    }

    void evaluate() {
        for (auto& item : markets_) {
            Book& up_book = books_[item.second.up]; Book& down_book = books_[item.second.down];
            if (!up_book.ws_snapshot || !down_book.ws_snapshot) {
                const double timestamp = now_seconds();
                if (item.second.last_reason != "book_uninitialized" || timestamp - item.second.last_audit >= 5) {
                    std::cout << "SHADOW_EVAL\tmarket=" << item.first << "\treason=book_uninitialized\tfok=0\n" << std::flush;
                    item.second.last_reason = "book_uninitialized";
                    item.second.last_audit = timestamp;
                }
                continue;
            }
            const double timestamp = now_seconds();
            const double up_age_ms = (timestamp - up_book.updated_at) * 1000, down_age_ms = (timestamp - down_book.updated_at) * 1000;
            const bool feed_fresh = timestamp - last_activity_ <= 30;
            const bool books_synced = feed_fresh;
            const double seconds_to_close = item.second.close_ts - timestamp;
            const double source_age_ms = std::max(std::abs(timestamp * 1000 - up_book.source_timestamp_ms), std::abs(timestamp * 1000 - down_book.source_timestamp_ms));
            auto up = buy_vwap(up_book, size_), down = buy_vwap(down_book, size_);
            const double up_best_ask = best_ask(up_book), down_best_ask = best_ask(down_book);
            const bool fok = up.first >= size_ && down.first >= size_;
            const double rate = item.second.fee > 0 ? item.second.fee : fallback_fee_;
            const double up_fee = std::round(up.first * rate * up.second * (1 - up.second) * 100000) / 100000;
            const double down_fee = std::round(down.first * rate * down.second * (1 - down.second) * 100000) / 100000;
            const double gross_cost = size_ * (up.second + down.second), buffer = size_ * buffer_per_share_;
            const double net_cost = gross_cost + up_fee + down_fee + buffer, profit = fok ? size_ - net_cost : 0;
            const double leg_1_fill_probability = books_synced ? std::min(1.0, up.first / size_) : 0;
            const double latency_decay = std::exp(-leg_interval_us_ / std::max(1.0, execution_half_life_us_));
            const double leg_2_fill_probability = books_synced ? std::min(1.0, down.first / size_) * latency_decay : 0;
            const double orphan_leg_loss = size_ * orphan_loss_per_share_;
            const double both_fill_probability = leg_1_fill_probability * leg_2_fill_probability;
            const double expected_execution_value = both_fill_probability * profit - leg_1_fill_probability * (1 - leg_2_fill_probability) * orphan_leg_loss;
            const bool good = fok && books_synced && seconds_to_close >= 20 && seconds_to_close <= 7200
                              && profit >= min_profit_ && expected_execution_value >= min_expected_value_;
            const std::string reason = !books_synced ? "books_not_synced" : seconds_to_close < 20 ? "closing_window" : seconds_to_close > 7200 ? "too_early" : up.first < size_ ? "up_depth" : down.first < size_ ? "down_depth" : profit < min_profit_ ? "net_cost_above_threshold" : expected_execution_value < min_expected_value_ ? "execution_value_below_threshold" : "opportunity";
            if (good && item.second.active_since == 0) item.second.active_since = timestamp;
            if (!good) item.second.active_since = 0;
            if (reason != item.second.last_reason || timestamp - item.second.last_audit >= 5) {
                const unsigned long long evaluation_sequence = ++evaluation_sequence_;
                const std::string evaluation_id = run_id_ + ":" + std::to_string(generation_) + ":" + std::to_string(ws_session_id_) + ":" + item.first + ":" + std::to_string(evaluation_sequence);
                std::cout << "SHADOW_EVAL\tmarket=" << item.first << "\treason=" << reason
                          << "\tfok=" << (fok ? 1 : 0) << "\tup_fill=" << up.first << "\tdown_fill=" << down.first
                          << "\tup_vwap=" << up.second << "\tdown_vwap=" << down.second
                          << "\tfees=" << up_fee + down_fee << "\tnet_cost=" << net_cost << "\tlocked_profit=" << profit << "\n" << std::flush;
                if (audit_) audit_ << "{\"ts\":" << timestamp << ",\"event_id\":\"" << evaluation_id << "\",\"run_id\":\"" << run_id_ << "\",\"evaluation_sequence\":" << evaluation_sequence << ",\"event_type\":\"shadow_eval\",\"strategy\":\"paired_lock\",\"market_id\":\"" << item.first
                                   << "\",\"reason\":\"" << reason << "\",\"fok\":" << (fok ? "true" : "false")
                                   << ",\"seconds_to_close\":" << seconds_to_close << ",\"size\":" << size_
                                   << ",\"subscription_generation\":" << generation_ << ",\"ws_session_id\":" << ws_session_id_
                                   << ",\"clock_skew_ms\":" << source_age_ms
                                   << ",\"clock_skew_basis\":\"clob_source_delta_upper_bound\",\"source_age_ms\":" << source_age_ms
                                   << ",\"up_book_age_ms\":" << up_age_ms << ",\"down_book_age_ms\":" << down_age_ms
                                   << ",\"up_fill\":" << up.first << ",\"down_fill\":" << down.first
                                   << ",\"up_available_depth\":" << available_ask_depth(up_book)
                                   << ",\"down_available_depth\":" << available_ask_depth(down_book)
                                   << ",\"up_vwap\":" << up.second << ",\"down_vwap\":" << down.second
                                   << ",\"up_best_ask\":" << up_best_ask << ",\"down_best_ask\":" << down_best_ask
                                   << ",\"up_slippage_per_share\":" << std::max(0.0, up.second - up_best_ask)
                                   << ",\"down_slippage_per_share\":" << std::max(0.0, down.second - down_best_ask)
                                   << ",\"up_book_imbalance\":" << book_imbalance(up_book)
                                   << ",\"down_book_imbalance\":" << book_imbalance(down_book)
                                   << ",\"up_fee\":" << up_fee << ",\"down_fee\":" << down_fee
                                   << ",\"fee_rate\":" << rate
                                   << ",\"gross_cost\":" << gross_cost << ",\"buffer\":" << buffer
                                   << ",\"net_cost\":" << net_cost << ",\"guaranteed_payout\":" << size_
                                   << ",\"locked_profit\":" << profit << ",\"locked_roi\":" << (net_cost > 0 ? profit / net_cost : 0)
                                   << ",\"leg_1_fill_probability\":" << leg_1_fill_probability
                                   << ",\"leg_2_fill_probability\":" << leg_2_fill_probability
                                   << ",\"time_between_legs_us\":" << leg_interval_us_
                                   << ",\"orphan_leg_loss\":" << orphan_leg_loss
                                   << ",\"expected_execution_value\":" << expected_execution_value
                                   << ",\"execution_model\":\"configured_latency_stress\""
                                   << ",\"books_synced\":" << (books_synced ? "true" : "false") << ",\"decision\":\"" << (good ? "ACCEPT" : "REJECT") << "\"}\n" << std::flush;
                item.second.last_reason = reason;
                item.second.last_audit = timestamp;
            }
            if (good) std::cout << "SHADOW_OPPORTUNITY\tmarket=" << item.first << "\tup_vwap=" << std::setprecision(12) << up.second
                                << "\tdown_vwap=" << down.second << "\tfees=" << up_fee + down_fee << "\tnet_cost=" << net_cost
                                << "\tprofit=" << profit << "\tfok=1\tduration_ms=" << (timestamp - item.second.active_since) * 1000 << "\n" << std::flush;
            if (good && audit_) audit_ << "{\"ts\":" << timestamp << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':' << ws_session_id_ << ':' << item.first << ":opportunity:" << ++opportunity_sequence_ << "\",\"run_id\":\"" << run_id_ << "\",\"event_type\":\"shadow_opportunity\",\"market_id\":\"" << item.first
                                       << "\",\"strategy\":\"paired_lock\",\"up_vwap\":" << up.second << ",\"down_vwap\":" << down.second
                                       << ",\"target_size\":" << size_ << ",\"gross_cost\":" << gross_cost
                                       << ",\"up_fee\":" << up_fee << ",\"down_fee\":" << down_fee
                                       << ",\"fee_rate\":" << rate << ",\"fees\":" << up_fee + down_fee << ",\"net_cost\":" << net_cost << ",\"guaranteed_payout\":" << size_ << ",\"locked_profit\":" << profit
                                       << ",\"fok\":true,\"duration_ms\":" << (timestamp - item.second.active_since) * 1000 << "}\n" << std::flush;
        }
        if (now_seconds() - last_health_write_ >= 1) write_health(true);
    }

    void write_health(bool connected) {
        size_t ready = 0, waiting_up = 0, waiting_down = 0;
        for (const auto& item : markets_) {
            const bool up_ready = books_[item.second.up].ws_snapshot;
            const bool down_ready = books_[item.second.down].ws_snapshot;
            if (up_ready && down_ready) ++ready;
            if (!up_ready) ++waiting_up;
            if (!down_ready) ++waiting_down;
        }
        const std::string temporary = health_path_ + ".tmp";
        std::ofstream out(temporary, std::ios::trunc);
        out << std::setprecision(15);
        out << "{\"updated_at\":" << now_seconds() << ",\"ws_connected\":" << (connected ? "true" : "false")
            << ",\"ws_session_id\":" << ws_session_id_ << ",\"subscription_generation\":" << generation_
            << ",\"document_version\":" << document_version_ << ",\"markets\":" << markets_.size()
            << ",\"tokens\":" << books_.size() << ",\"ready_markets\":" << ready
            << ",\"waiting_up_snapshot\":" << waiting_up << ",\"waiting_down_snapshot\":" << waiting_down
            << ",\"last_market_data_at\":" << last_activity_ << ",\"full_resyncs\":" << full_resync_count_
            << ",\"book_events\":" << book_events_ << ",\"price_changes\":" << price_changes_ << "}\n";
        out.close();
        std::filesystem::rename(temporary, health_path_);
        last_health_write_ = now_seconds();
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

    void schedule_reload() {
        reload_timer_.expires_after(std::chrono::seconds(5));
        reload_timer_.async_wait([self = shared_from_this()](beast::error_code ec) {
            if (ec || self->stopped_) return;
            self->reload_markets();
            self->schedule_reload();
        });
    }

    void resync_token(const std::string& token, const std::string& reason) {
        auto found = books_.find(token);
        if (found == books_.end()) return;
        found->second.ws_snapshot = false;
        ++full_resync_count_;
        std::cerr << "BOOK_RESYNC token=" << token << " reason=" << reason << " count=" << full_resync_count_ << "\n";
        queue_write(subscription({token}, "unsubscribe"));
        queue_write(subscription({token}, "subscribe"));
    }

    void reload_markets() {
        try {
            unsigned long long next_version = 0;
            auto next = load_markets(markets_path_, &next_version);
            if (next_version <= document_version_) return;
            std::map<std::string, bool> old_tokens, next_tokens;
            for (const auto& item : markets_) { old_tokens[item.second.up] = true; old_tokens[item.second.down] = true; }
            for (auto& item : next) {
                next_tokens[item.second.up] = true; next_tokens[item.second.down] = true;
                auto old = markets_.find(item.first);
                if (old != markets_.end()) {
                    item.second.active_since = old->second.active_since;
                    item.second.last_audit = old->second.last_audit;
                    item.second.last_reason = old->second.last_reason;
                }
            }
            std::vector<std::string> added, removed;
            ++generation_;
            for (auto& book : books_) book.second.generation = generation_;
            for (const auto& token : next_tokens) if (!old_tokens.count(token.first)) {
                books_[token.first] = Book{};
                books_[token.first].generation = generation_;
                added.push_back(token.first);
            }
            for (const auto& token : old_tokens) if (!next_tokens.count(token.first)) removed.push_back(token.first);
            markets_ = std::move(next);
            document_version_ = next_version;
            if (!added.empty()) queue_write(subscription(added, "subscribe"));
            if (!removed.empty()) queue_write(subscription(removed, "unsubscribe"));
            for (const auto& token : removed) books_.erase(token);
            if (!added.empty() || !removed.empty())
                std::cerr << "MARKET_RELOAD markets=" << markets_.size() << " subscribe=" << added.size() << " unsubscribe=" << removed.size() << "\n";
        } catch (const std::exception& error) {
            std::cerr << "MARKET_RELOAD_ERROR message=" << error.what() << "\n";
        }
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
        stopped_ = true; timer_.cancel(); reload_timer_.cancel();
        write_health(false);
        std::cerr << "WS_ERROR stage=" << stage << " code=" << ec.value() << " message=" << ec.message() << "\n";
    }

    const std::string host_ = "ws-subscriptions-clob.polymarket.com";
    asio::io_context& io_;
    asio::ssl::context& ssl_;
    tcp::resolver resolver_;
    websocket::stream<ssl_socket> ws_;
    beast::flat_buffer buffer_;
    asio::steady_timer timer_;
    asio::steady_timer reload_timer_;
    std::deque<std::string> writes_;
    std::map<std::string, Market> markets_;
    std::map<std::string, Book> books_;
    double size_, fallback_fee_, buffer_per_share_, min_profit_, leg_interval_us_, execution_half_life_us_;
    double orphan_loss_per_share_, min_expected_value_, last_activity_;
    unsigned long long book_events_ = 0, price_changes_ = 0;
    bool stopped_ = false;
    std::ofstream audit_;
    std::string markets_path_, health_path_, run_id_;
    double last_health_write_ = 0;
    unsigned long long document_version_, generation_, ws_session_id_, full_resync_count_ = 0;
    unsigned long long evaluation_sequence_ = 0, opportunity_sequence_ = 0;
    static std::atomic<unsigned long long> next_session_id_;
};

std::atomic<unsigned long long> MarketWsSession::next_session_id_{0};

int main(int argc, char** argv) {
    if (argc < 2) { std::cerr << "usage: market_ws_engine <markets.json> [size] [fallback_fee_rate] [audit.jsonl] [buffer_per_share] [min_profit] [leg_interval_us] [execution_half_life_us] [orphan_loss_per_share] [min_expected_value] [health.json]\n"; return 2; }
    try {
        unsigned long long document_version = 0;
        std::map<std::string, Market> markets = load_markets(argv[1], &document_version);
        if (markets.empty()) { std::cerr << "NO_TOKENS live_markets.json contains no valid Up/Down tokens\n"; return 4; }
        const double size = argc > 2 ? std::stod(argv[2]) : 10, fee = argc > 3 ? std::stod(argv[3]) : .07;
        const std::string audit_path = argc > 4 ? argv[4] : "logs/shadow-audit.jsonl";
        const double buffer_per_share = argc > 5 ? std::stod(argv[5]) : .002;
        const double min_profit = argc > 6 ? std::stod(argv[6]) : .01;
        const double leg_interval_us = argc > 7 ? std::stod(argv[7]) : 50000;
        const double execution_half_life_us = argc > 8 ? std::stod(argv[8]) : 250000;
        const double orphan_loss_per_share = argc > 9 ? std::stod(argv[9]) : .02;
        const double min_expected_value = argc > 10 ? std::stod(argv[10]) : .01;
        const std::string health_path = argc > 11 ? argv[11] : "data/shadow-health.json";
        for (;;) {
            asio::io_context io; asio::ssl::context ssl(asio::ssl::context::tls_client);
            ssl.set_default_verify_paths(); ssl.set_verify_mode(asio::ssl::verify_peer);
            std::map<std::string, Book> books;
            for (const auto& item : markets) { books[item.second.up]; books[item.second.down]; }
            std::cerr << "BOOK_BOOTSTRAP_SKIPPED reason=ws_snapshot_required tokens=" << books.size() << "\n";
            auto session = std::make_shared<MarketWsSession>(io, ssl, markets, std::move(books), size, fee, buffer_per_share,
                min_profit, leg_interval_us, execution_half_life_us, orphan_loss_per_share, min_expected_value,
                audit_path, argv[1], document_version, health_path);
            session->run(); io.run();
            std::cerr << "WS_RECONNECT delay_s=2\n";
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    } catch (const std::exception& error) { std::cerr << "FATAL " << error.what() << "\n"; return 1; }
}
