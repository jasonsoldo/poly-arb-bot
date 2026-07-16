#include <boost/asio/connect.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/ssl.hpp>
#include <boost/asio/steady_timer.hpp>
#include <boost/beast/core.hpp>
#include <boost/beast/ssl.hpp>
#include <boost/beast/websocket.hpp>
#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>
#include "../reference_ipc/latest_value_client.hpp"
#include "../strategy/complete_set_arb.hpp"
#include "../strategy/ev_strategy.hpp"
#include <openssl/sha.h>
#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdlib>
#include <deque>
#include <fstream>
#include <filesystem>
#include <iomanip>
#include <iostream>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
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
    unsigned long long generation = 0, version = 0;
};
struct Market {
    std::string up, down, last_reason, condition_id, asset, interval, window;
    std::string settlement_source, title, open_price_source, open_price_capture_mode;
    std::optional<double> open_price, open_price_source_timestamp_ms;
    double fee = .07, start_ts = 0, close_ts = 0, active_since = 0, last_audit = 0;
    unsigned long long last_strategy_up_version = 0, last_strategy_down_version = 0;
    unsigned long long last_strategy_reference_revision = 0, last_strategy_time_bucket = 0;
    bool accepting_orders = true;
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
        if (const auto value = row.get_optional<double>("start_ts")) market.start_ts = *value;
        market.condition_id = row.get<std::string>("condition_id", row.get<std::string>("market_id", ""));
        market.asset = row.get<std::string>("asset", "");
        market.interval = row.get<std::string>("interval", "");
        market.settlement_source = row.get<std::string>("settlement_source", "");
        if (market.settlement_source == "null") market.settlement_source.clear();
        market.title = row.get<std::string>("title", "");
        market.accepting_orders = row.get<bool>("accepting_orders", true);
        if (const auto value = row.get_optional<double>("open_price")) market.open_price = *value;
        market.open_price_source = row.get<std::string>("open_price_source", "");
        if (market.open_price_source == "null") market.open_price_source.clear();
        market.open_price_capture_mode = row.get<std::string>("open_price_capture_mode", "");
        if (market.open_price_capture_mode == "null") market.open_price_capture_mode.clear();
        if (const auto value = row.get_optional<double>("open_price_source_timestamp_ms"))
            market.open_price_source_timestamp_ms = *value;
        market.window = row.get<std::string>("window", market.start_ts > now_seconds() ? "next" : "current");
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

std::string environment_value(const char* name, const char* fallback) {
    const char* value = std::getenv(name);
    return value && *value ? value : fallback;
}

double environment_double(const char* name, const char* fallback) {
    return std::stod(environment_value(name, fallback));
}

strategy::Config strategy_config_from_environment() {
    strategy::Config config;
    config.directional_min_net_ev = environment_double("DIRECTIONAL_MIN_NET_EV", "0.015");
    config.directional_min_probability = environment_double("DIRECTIONAL_MIN_PROBABILITY", "0.90");
    config.directional_latency_buffer = environment_double("DIRECTIONAL_LATENCY_BUFFER", "0.003");
    config.directional_settlement_buffer = environment_double("DIRECTIONAL_SETTLEMENT_BUFFER", "0.002");
    config.lottery_min_price = environment_double("LOTTERY_MIN_PRICE", "0.01");
    config.lottery_max_price = environment_double("LOTTERY_MAX_PRICE", "0.05");
    config.lottery_min_net_ev = environment_double("LOTTERY_MIN_NET_EV", "0.015");
    config.lottery_model_buffer = environment_double("LOTTERY_MODEL_BUFFER", "0.01");
    config.lottery_execution_buffer = environment_double("LOTTERY_EXECUTION_BUFFER", "0.005");
    config.minimum_liquidity = environment_double("STRATEGY_MIN_LIQUIDITY", "20");
    config.maximum_slippage = environment_double("STRATEGY_MAX_SLIPPAGE", "0.01");
    config.maximum_reference_age_ms = environment_double("REFERENCE_MAX_AGE_MS", "3000");
    config.maximum_book_age_ms = environment_double("CLOB_MAX_BOOK_AGE_MS", "750");
    config.maximum_clock_skew_ms = environment_double("MAX_CLOCK_SKEW_MS", "250");
    config.momentum_z_per_bps = environment_double("MODEL_MOMENTUM_Z_PER_BPS", "0.002");
    config.imbalance_z = environment_double("MODEL_IMBALANCE_Z", "0.25");
    config.lottery_distance_weight = environment_double("LOTTERY_DISTANCE_WEIGHT", "1.0");
    config.lottery_momentum_z_per_bps = environment_double("LOTTERY_MOMENTUM_Z_PER_BPS", "0.001");
    config.lottery_imbalance_z = environment_double("LOTTERY_IMBALANCE_Z", "0.10");
    config.lottery_market_blend = environment_double("LOTTERY_MARKET_BLEND", "0.50");
    config.minimum_model_sample_span_seconds = environment_double("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60");
    config.terminal_hedge_max_reversal_loss = environment_double("TERMINAL_HEDGE_MAX_REVERSAL_LOSS", "1.0");
    config.terminal_hedge_min_expected_pnl = environment_double("TERMINAL_HEDGE_MIN_EXPECTED_PNL", "0.05");
    config.terminal_hedge_max_size_ratio = environment_double("TERMINAL_HEDGE_MAX_SIZE_RATIO", "1.0");
    config.directional_windows = {
        {"5m", {static_cast<int>(environment_double("DIRECTIONAL_WINDOW_5M_MIN", "5")), static_cast<int>(environment_double("DIRECTIONAL_WINDOW_5M_MAX", "15"))}},
        {"15m", {static_cast<int>(environment_double("DIRECTIONAL_WINDOW_15M_MIN", "5")), static_cast<int>(environment_double("DIRECTIONAL_WINDOW_15M_MAX", "20"))}},
        {"1h", {static_cast<int>(environment_double("DIRECTIONAL_WINDOW_1H_MIN", "8")), static_cast<int>(environment_double("DIRECTIONAL_WINDOW_1H_MAX", "30"))}},
        {"4h", {static_cast<int>(environment_double("DIRECTIONAL_WINDOW_4H_MIN", "10")), static_cast<int>(environment_double("DIRECTIONAL_WINDOW_4H_MAX", "45"))}},
    };
    return config;
}

std::string sha256_hex(const std::string& payload) {
    unsigned char digest[SHA256_DIGEST_LENGTH];
    SHA256(reinterpret_cast<const unsigned char*>(payload.data()), payload.size(), digest);
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (unsigned char byte : digest) output << std::setw(2) << static_cast<int>(byte);
    return output.str();
}

std::string strategy_config_hash(const std::string& strategy_name = "") {
    std::map<std::string, std::string> values = {
        {"coinbase_reference_max_age_ms", environment_value("COINBASE_REFERENCE_MAX_AGE_MS", "10000")},
        {"directional_latency_buffer", environment_value("DIRECTIONAL_LATENCY_BUFFER", "0.003")},
        {"directional_min_net_ev", environment_value("DIRECTIONAL_MIN_NET_EV", "0.015")},
        {"directional_min_probability", environment_value("DIRECTIONAL_MIN_PROBABILITY", "0.90")},
        {"directional_window_5m_min", environment_value("DIRECTIONAL_WINDOW_5M_MIN", "5")},
        {"directional_window_5m_max", environment_value("DIRECTIONAL_WINDOW_5M_MAX", "15")},
        {"directional_window_15m_min", environment_value("DIRECTIONAL_WINDOW_15M_MIN", "5")},
        {"directional_window_15m_max", environment_value("DIRECTIONAL_WINDOW_15M_MAX", "20")},
        {"directional_window_1h_min", environment_value("DIRECTIONAL_WINDOW_1H_MIN", "8")},
        {"directional_window_1h_max", environment_value("DIRECTIONAL_WINDOW_1H_MAX", "30")},
        {"directional_window_4h_min", environment_value("DIRECTIONAL_WINDOW_4H_MIN", "10")},
        {"directional_window_4h_max", environment_value("DIRECTIONAL_WINDOW_4H_MAX", "45")},
        {"directional_settlement_buffer", environment_value("DIRECTIONAL_SETTLEMENT_BUFFER", "0.002")},
        {"imbalance_z", environment_value("MODEL_IMBALANCE_Z", "0.25")},
        {"inventory_max_complement_gap", environment_value("INVENTORY_MAX_COMPLEMENT_GAP", "0.03")},
        {"inventory_max_initial_price", environment_value("INVENTORY_MAX_INITIAL_PRICE", "0.20")},
        {"inventory_max_total_unmatched_notional", environment_value("INVENTORY_MAX_TOTAL_UNMATCHED_NOTIONAL", "3.0")},
        {"inventory_max_unmatched_notional", environment_value("INVENTORY_MAX_UNMATCHED_NOTIONAL", "0.50")},
        {"inventory_min_entry_edge", environment_value("INVENTORY_MIN_ENTRY_EDGE", "0.05")},
        {"inventory_min_entry_ev_roi", environment_value("INVENTORY_MIN_ENTRY_EV_ROI", "0.25")},
        {"inventory_min_locked_roi", environment_value("INVENTORY_MIN_LOCKED_ROI", "0.02")},
        {"lottery_execution_buffer", environment_value("LOTTERY_EXECUTION_BUFFER", "0.005")},
        {"lottery_max_price", environment_value("LOTTERY_MAX_PRICE", "0.05")},
        {"lottery_min_net_ev", environment_value("LOTTERY_MIN_NET_EV", "0.015")},
        {"lottery_min_price", environment_value("LOTTERY_MIN_PRICE", "0.01")},
        {"lottery_model_buffer", environment_value("LOTTERY_MODEL_BUFFER", "0.01")},
        {"lottery_distance_weight", environment_value("LOTTERY_DISTANCE_WEIGHT", "1.0")},
        {"lottery_momentum_z_per_bps", environment_value("LOTTERY_MOMENTUM_Z_PER_BPS", "0.001")},
        {"lottery_imbalance_z", environment_value("LOTTERY_IMBALANCE_Z", "0.10")},
        {"lottery_market_blend", environment_value("LOTTERY_MARKET_BLEND", "0.50")},
        {"maximum_book_age_ms", environment_value("CLOB_MAX_BOOK_AGE_MS", "750")},
        {"maximum_clock_skew_ms", environment_value("MAX_CLOCK_SKEW_MS", "250")},
        {"maximum_reference_age_ms", environment_value("REFERENCE_MAX_AGE_MS", "3000")},
        {"maximum_slippage", environment_value("STRATEGY_MAX_SLIPPAGE", "0.01")},
        {"maker_both_fill_probability", environment_value("MAKER_BOTH_FILL_PROBABILITY", "0")},
        {"maker_expected_rebate_per_pair", environment_value("MAKER_EXPECTED_REBATE_PER_PAIR", "0")},
        {"maker_inventory_skew_per_unit", environment_value("MAKER_INVENTORY_SKEW_PER_UNIT", "0.005")},
        {"maker_minimum_pair_edge", environment_value("MAKER_MINIMUM_PAIR_EDGE", "0.01")},
        {"maker_orphan_loss", environment_value("MAKER_ORPHAN_LOSS", "0.02")},
        {"maker_quote_half_spread", environment_value("MAKER_QUOTE_HALF_SPREAD", "0.02")},
        {"maker_tick_size", environment_value("MAKER_TICK_SIZE", "0.01")},
        {"minimum_liquidity", environment_value("STRATEGY_MIN_LIQUIDITY", "20")},
        {"minimum_model_sample_span_seconds", environment_value("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60")},
        {"terminal_hedge_max_reversal_loss", environment_value("TERMINAL_HEDGE_MAX_REVERSAL_LOSS", "1.0")},
        {"terminal_hedge_min_expected_pnl", environment_value("TERMINAL_HEDGE_MIN_EXPECTED_PNL", "0.05")},
        {"terminal_hedge_max_size_ratio", environment_value("TERMINAL_HEDGE_MAX_SIZE_RATIO", "1.0")},
        {"momentum_z_per_bps", environment_value("MODEL_MOMENTUM_Z_PER_BPS", "0.002")},
        {"probability_reference", "settlement_reference"},
        {"shadow_buffer_per_share", environment_value("SHADOW_BUFFER_PER_SHARE", "0.002")},
        {"shadow_min_profit", environment_value("SHADOW_MIN_PROFIT", "0.01")},
        {"shadow_size", environment_value("SHADOW_SIZE", "10")},
    };
    if (!strategy_name.empty()) {
        const auto relevant = [&](const std::string& key) {
            const bool common = key == "coinbase_reference_max_age_ms" ||
                key == "minimum_liquidity" || key == "maximum_slippage" ||
                key == "maximum_reference_age_ms" || key == "maximum_book_age_ms" ||
                key == "maximum_clock_skew_ms" || key == "minimum_model_sample_span_seconds" ||
                key == "probability_reference" || key.rfind("terminal_hedge_", 0) == 0;
            if (strategy_name == "late_window_directional_ev") return common ||
                key == "directional_min_net_ev" || key == "directional_latency_buffer" ||
                key == "directional_settlement_buffer" || key == "directional_min_probability" ||
                key.rfind("directional_window_", 0) == 0 || key == "momentum_z_per_bps" ||
                key == "imbalance_z";
            if (strategy_name == "low_price_lottery_ev") return common ||
                key == "lottery_min_price" || key == "lottery_max_price" ||
                key == "lottery_min_net_ev" || key == "lottery_model_buffer" ||
                key == "lottery_execution_buffer" || key == "lottery_distance_weight" ||
                key == "lottery_momentum_z_per_bps" || key == "lottery_imbalance_z" ||
                key == "lottery_market_blend";
            if (strategy_name == "inventory_rebalancing_arb") return common ||
                key.rfind("inventory_", 0) == 0 || key.rfind("shadow_", 0) == 0 ||
                key == "directional_latency_buffer" ||
                key == "directional_settlement_buffer" ||
                key == "momentum_z_per_bps" || key == "imbalance_z";
            if (strategy_name == "maker_complete_set_arb") return common ||
                key.rfind("maker_", 0) == 0 || key.rfind("shadow_", 0) == 0 ||
                key == "momentum_z_per_bps" || key == "imbalance_z";
            return true;
        };
        for (auto item = values.begin(); item != values.end();) {
            if (!relevant(item->first)) item = values.erase(item);
            else ++item;
        }
        values["strategy"] = strategy_name;
    }
    std::ostringstream encoded;
    encoded << '{';
    bool first = true;
    for (const auto& item : values) {
        if (!first) encoded << ',';
        first = false;
        encoded << '"' << item.first << "\":\"" << reference_ipc::escaped(item.second) << '"';
    }
    encoded << '}';
    return sha256_hex(encoded.str());
}

std::string paired_config_hash(double size, double fallback_fee, double buffer_per_share,
                               double min_profit, double leg_interval_us,
                               double execution_half_life_us, double orphan_loss_per_share,
                               double min_expected_value) {
    std::ostringstream encoded;
    encoded << std::setprecision(17)
            << "{\"buffer_per_share\":" << buffer_per_share
            << ",\"execution_half_life_us\":" << execution_half_life_us
            << ",\"fallback_fee_rate\":" << fallback_fee
            << ",\"leg_interval_us\":" << leg_interval_us
            << ",\"maximum_book_age_ms\":750"
            << ",\"maximum_seconds_to_close\":7200"
            << ",\"minimum_expected_execution_value\":" << min_expected_value
            << ",\"minimum_locked_profit\":" << min_profit
            << ",\"minimum_seconds_to_close\":20"
            << ",\"orphan_loss_per_share\":" << orphan_loss_per_share
            << ",\"target_size\":" << size << '}';
    return sha256_hex(encoded.str());
}

double median(std::vector<double> values) {
    if (values.empty()) return 0;
    std::sort(values.begin(), values.end());
    const std::size_t middle = values.size() / 2;
    return values.size() % 2 ? values[middle] : (values[middle - 1] + values[middle]) / 2;
}

struct EffectiveReferenceSource {
    std::string source, symbol, market_type, quote_currency, status;
    std::optional<double> price;
    std::optional<double> age_ms;
};

template <std::size_t Capacity>
class RollingMetric {
public:
    void add(double value) {
        if (!std::isfinite(value) || value < 0) return;
        values_[next_] = value;
        next_ = (next_ + 1) % Capacity;
        count_ = std::min(count_ + 1, Capacity);
        latest_ = value;
    }

    double latest() const { return latest_; }
    std::size_t count() const { return count_; }

    double percentile(double fraction) const {
        if (!count_) return 0;
        std::vector<double> rows(values_.begin(), values_.begin() + count_);
        const std::size_t index = std::min(
            rows.size() - 1,
            static_cast<std::size_t>(std::llround((rows.size() - 1) * fraction)));
        std::nth_element(rows.begin(), rows.begin() + index, rows.end());
        return rows[index];
    }

private:
    std::array<double, Capacity> values_{};
    std::size_t next_ = 0, count_ = 0;
    double latest_ = 0;
};

struct ReferenceView {
    std::vector<EffectiveReferenceSource> sources;
    std::optional<double> fast_price, consensus_price, settlement_reference;
    std::optional<double> divergence_bps, reference_age_ms, clock_skew_ms;
    int fresh_exchange_sources = 0, fresh_usd_sources = 0;
    bool quorum = false, settlement_verified = false;
    std::string state = "REFERENCE_BLOCKED", reason = "insufficient_reference_sources";
    double maximum_reference_age_ms = 3000;
};

ReferenceView build_reference_view(const reference_ipc::AssetSnapshot& asset,
                                   const std::string& settlement_source,
                                   double transport_age_ms, const strategy::Config& config) {
    ReferenceView view;
    view.maximum_reference_age_ms = config.maximum_reference_age_ms;
    view.fast_price = asset.fast_price;
    view.clock_skew_ms = asset.clock_skew_ms;
    std::vector<double> usd_prices;
    for (const auto& item : asset.sources) {
        const auto& source = item.second;
        const double maximum_age = item.first == "coinbase"
            ? environment_double("COINBASE_REFERENCE_MAX_AGE_MS", "10000")
            : config.maximum_reference_age_ms;
        std::optional<double> age;
        if (source.message_age_ms) age = *source.message_age_ms + transport_age_ms;
        std::string status = source.status;
        if (status == "FRESH" && (!age || *age > maximum_age)) status = "STALE";
        view.sources.push_back({item.first, source.symbol, source.market_type,
                                source.quote_currency, status, source.price, age});
        if (status == "FRESH" && source.market_type == "spot" &&
            source.quote_currency == "USD" && source.price) usd_prices.push_back(*source.price);
    }
    const double usd_center = median(usd_prices);
    for (auto& source : view.sources) {
        if (source.status == "FRESH" && source.market_type == "spot" &&
            source.quote_currency == "USD" && source.price && usd_center > 0 &&
            std::abs(*source.price - usd_center) / usd_center * 10000 > 100) {
            source.status = "OUTLIER";
        }
    }
    std::vector<double> valid_prices, valid_usd, reference_ages;
    for (const auto& source : view.sources) {
        const bool valid_spot = source.status == "FRESH" && source.market_type == "spot" && source.price;
        if (valid_spot) {
            valid_prices.push_back(*source.price);
            ++view.fresh_exchange_sources;
            if (source.quote_currency == "USD") {
                valid_usd.push_back(*source.price);
                ++view.fresh_usd_sources;
            }
        }
        if (source.source == settlement_source && source.status == "FRESH" && source.price) {
            view.settlement_reference = source.price;
            view.settlement_verified = true;
        }
        if (source.status == "FRESH" && source.age_ms &&
            (source.market_type == "spot" || source.source == settlement_source)) {
            reference_ages.push_back(*source.age_ms);
            view.maximum_reference_age_ms = std::max(
                view.maximum_reference_age_ms,
                source.source == "coinbase"
                    ? environment_double("COINBASE_REFERENCE_MAX_AGE_MS", "10000")
                    : config.maximum_reference_age_ms);
        }
    }
    if (!valid_usd.empty()) view.consensus_price = median(valid_usd);
    if (!reference_ages.empty()) view.reference_age_ms = *std::max_element(reference_ages.begin(), reference_ages.end());
    if (valid_prices.size() > 1) {
        const auto bounds = std::minmax_element(valid_prices.begin(), valid_prices.end());
        const double center = median(valid_prices);
        if (center > 0) view.divergence_bps = (*bounds.second - *bounds.first) / center * 10000;
    }
    if (view.fresh_exchange_sources < 2) view.reason = "insufficient_reference_sources";
    else if (view.fresh_usd_sources < 1) view.reason = "required_usd_spot_source_unavailable";
    else if (!view.settlement_verified) view.reason = "settlement_reference_unavailable";
    else if (view.divergence_bps && *view.divergence_bps > 100) view.reason = "cross_source_divergence_exceeded";
    else { view.quorum = true; view.state = "REFERENCE_READY"; view.reason.clear(); }
    return view;
}

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
double best_bid(const Book& book) { return book.bids.empty() ? 0 : book.bids.rbegin()->first; }

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

class BoundedAuditWriter {
public:
    explicit BoundedAuditWriter(const std::string& path, std::size_t capacity = 4096)
        : output_(path, std::ios::app), capacity_(capacity), worker_([this] { run(); }) {
        failed_.store(!output_);
    }

    ~BoundedAuditWriter() {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        condition_.notify_one();
        if (worker_.joinable()) worker_.join();
    }

    bool enqueue(std::string line) {
        if (failed_.load()) return false;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            if (queue_.size() >= capacity_) return false;
            queue_.push_back(std::move(line));
        }
        condition_.notify_one();
        return true;
    }

    bool available() const {
        if (failed_.load()) return false;
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.size() < capacity_;
    }
    std::size_t queued() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.size();
    }

private:
    void run() {
        for (;;) {
            std::deque<std::string> batch;
            {
                std::unique_lock<std::mutex> lock(mutex_);
                condition_.wait(lock, [this] { return stopping_ || !queue_.empty(); });
                if (stopping_ && queue_.empty()) break;
                batch.swap(queue_);
            }
            for (const auto& line : batch) output_ << line;
            output_.flush();
            if (!output_) failed_.store(true);
        }
    }

    std::ofstream output_;
    const std::size_t capacity_;
    mutable std::mutex mutex_;
    std::condition_variable condition_;
    std::deque<std::string> queue_;
    bool stopping_ = false;
    std::atomic<bool> failed_{false};
    std::thread worker_;
};

class MarketWsSession : public std::enable_shared_from_this<MarketWsSession> {
public:
    MarketWsSession(asio::io_context& io, asio::ssl::context& ssl, std::map<std::string, Market> markets,
                    std::map<std::string, Book> books, double size, double fee, double buffer_per_share,
                    double min_profit, double leg_interval_us, double execution_half_life_us,
                    double orphan_loss_per_share, double min_expected_value, const std::string& audit_path,
                    const std::string& markets_path, unsigned long long document_version, const std::string& health_path,
                    const std::string& strategy_audit_path)
        : io_(io), ssl_(ssl), resolver_(io), ws_(io, ssl), timer_(io), reload_timer_(io), evaluation_timer_(io), markets_(std::move(markets)), books_(std::move(books)),
          size_(size), fallback_fee_(fee), buffer_per_share_(buffer_per_share), min_profit_(min_profit),
          leg_interval_us_(leg_interval_us), execution_half_life_us_(execution_half_life_us),
          orphan_loss_per_share_(orphan_loss_per_share), min_expected_value_(min_expected_value),
          last_activity_(now_seconds()), audit_(audit_path, std::ios::app), strategy_audit_(strategy_audit_path),
          markets_path_(markets_path), health_path_(health_path),
          run_id_(std::to_string(static_cast<unsigned long long>(now_seconds() * 1000000))),
          document_version_(document_version), generation_(1), ws_session_id_(++next_session_id_) {
        audit_ << std::setprecision(15);
        paired_config_hash_ = paired_config_hash(
            size_, fallback_fee_, buffer_per_share_, min_profit_, leg_interval_us_,
            execution_half_life_us_, orphan_loss_per_share_, min_expected_value_);
        inventory_strategy_config_hash_ = sha256_hex(
            inventory_strategy_config_hash_ + "|" + std::to_string(size_) + "|" +
            std::to_string(buffer_per_share_) + "|" + std::to_string(min_profit_));
        maker_strategy_config_hash_ = sha256_hex(
            maker_strategy_config_hash_ + "|" + std::to_string(size_) + "|" +
            std::to_string(buffer_per_share_));
        load_complete_set_inventory();
        strategy_accept_heartbeat_seconds_ = environment_double("STRATEGY_ACCEPT_AUDIT_HEARTBEAT_SECONDS", "5");
        strategy_reject_heartbeat_seconds_ = environment_double("STRATEGY_REJECT_AUDIT_HEARTBEAT_SECONDS", "60");
        for (auto& item : books_) item.second.generation = generation_;
    }

    void run() {
        const char* configured_path = std::getenv("REFERENCE_IPC_PATH");
        const std::string reference_path = configured_path && *configured_path
            ? configured_path : "state/reference-price.sock";
        std::weak_ptr<MarketWsSession> weak_self = shared_from_this();
        reference_client_ = std::make_shared<reference_ipc::LatestValueClient>(
            io_, reference_path,
            [weak_self](const reference_ipc::Snapshot& snapshot) {
                if (auto self = weak_self.lock()) self->on_reference_snapshot(snapshot);
            },
            [weak_self](bool connected) {
                if (auto self = weak_self.lock()) self->on_reference_state(connected);
            });
        reference_client_->start();
        resolver_.async_resolve(host_, "443", beast::bind_front_handler(&MarketWsSession::on_resolve, shared_from_this()));
    }

private:
    void on_reference_snapshot(const reference_ipc::Snapshot& snapshot) {
        reference_snapshot_ = snapshot;
        reference_receive_at_ = std::chrono::steady_clock::now();
        const auto receive_ns = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
            reference_receive_at_.time_since_epoch()).count());
        reference_transport_age_at_receive_ms_ = reference_ipc::transport_age_ms(
            snapshot.produced_monotonic_ns, receive_ns, 0);
        reference_ipc_receive_age_ms_.add(reference_transport_age_at_receive_ms_);
        evaluate_reference_strategies();
    }

    void on_reference_state(bool connected) {
        reference_connected_ = connected;
    }

    std::optional<double> price_to_beat(const Market& market,
                                        const reference_ipc::AssetSnapshot* asset) const {
        if (market.open_price) return market.open_price;
        if (!asset || market.start_ts <= 0 || now_seconds() < market.start_ts) return std::nullopt;
        const auto found = asset->sources.find(market.settlement_source);
        if (found == asset->sources.end()) return std::nullopt;
        const double start_ms = market.start_ts * 1000;
        std::optional<reference_ipc::AnchorSample> best;
        const auto consider = [&](const std::vector<reference_ipc::AnchorSample>& samples) {
            for (const auto& sample : samples) {
                if (sample.source_timestamp_ms < start_ms || sample.source_timestamp_ms > start_ms + 10000)
                    continue;
                if (!sample.timeframe.empty() && sample.timeframe != market.interval) continue;
                if (!best || sample.source_timestamp_ms < best->source_timestamp_ms) best = sample;
            }
        };
        consider(found->second.anchor_samples);
        consider(found->second.settlement_samples);
        return best ? std::optional<double>(best->price) : std::nullopt;
    }

    bool should_emit_strategy(const std::string& key, const strategy::Decision& decision, double timestamp) {
        const std::string fingerprint = decision.decision + "|" + decision.reason + "|" +
            strategy_hash_for(decision.strategy);
        const auto found = strategy_emission_state_.find(key);
        const double heartbeat = decision.decision == "ACCEPT"
            ? strategy_accept_heartbeat_seconds_ : strategy_reject_heartbeat_seconds_;
        if (found != strategy_emission_state_.end() && found->second.first == fingerprint &&
            timestamp - found->second.second < heartbeat) return false;
        strategy_emission_state_[key] = {fingerprint, timestamp};
        return true;
    }

    const std::string& strategy_hash_for(const std::string& strategy_name) const {
        return strategy_name == "low_price_lottery_ev"
            ? lottery_strategy_config_hash_ : directional_strategy_config_hash_;
    }

    void evaluate_reference_strategies() {
        if (stopped_ || reference_snapshot_.producer_session.empty()) {
            last_clob_mutation_at_ = {};
            return;
        }
        bool evaluated_any = false;
        const double timestamp = now_seconds();
        const unsigned long long time_bucket = static_cast<unsigned long long>(timestamp);
        const double transport_age_ms = reference_receive_at_.time_since_epoch().count()
            ? reference_transport_age_at_receive_ms_ + std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - reference_receive_at_).count()
            : 1e9;
        for (auto& item : markets_) {
            Market& market = item.second;
            const Book& up_book = books_.at(market.up);
            const Book& down_book = books_.at(market.down);
            if (!up_book.ws_snapshot || !down_book.ws_snapshot) continue;
            const auto asset_found = reference_snapshot_.assets.find(market.asset);
            const reference_ipc::AssetSnapshot* asset = asset_found == reference_snapshot_.assets.end()
                ? nullptr : &asset_found->second;
            const unsigned long long reference_revision = asset ? asset->revision : 0;
            const bool inputs_unchanged =
                market.last_strategy_up_version == up_book.version &&
                market.last_strategy_down_version == down_book.version &&
                market.last_strategy_reference_revision == reference_revision &&
                market.last_strategy_time_bucket == time_bucket;
            if (inputs_unchanged) continue;
            market.last_strategy_up_version = up_book.version;
            market.last_strategy_down_version = down_book.version;
            market.last_strategy_reference_revision = reference_revision;
            market.last_strategy_time_bucket = time_bucket;
            evaluated_any = true;
            const ReferenceView reference = asset
                ? build_reference_view(*asset, market.settlement_source, transport_age_ms, strategy_config_)
                : ReferenceView{};
            const double up_age_ms = std::max(0.0, (timestamp - up_book.updated_at) * 1000);
            const double down_age_ms = std::max(0.0, (timestamp - down_book.updated_at) * 1000);
            const double book_state_age_ms = std::max(up_age_ms, down_age_ms);
            const double clob_feed_age_ms = std::max(0.0, (timestamp - last_activity_) * 1000);
            const double book_age_ms = std::min(book_state_age_ms, clob_feed_age_ms);
            const int seconds_to_close = std::max(0, static_cast<int>(market.close_ts - timestamp));
            const auto opening_price = price_to_beat(market, asset);
            const double paired_imbalance = (book_imbalance(up_book) - book_imbalance(down_book)) / 2;
            strategy::ProbabilityInput probability_input;
            probability_input.settlement_reference = reference.settlement_reference;
            probability_input.price_to_beat = opening_price;
            probability_input.seconds_to_close = seconds_to_close;
            if (asset) {
                probability_input.volatility_per_sqrt_second = asset->volatility_per_sqrt_second;
                probability_input.model_sample_count = asset->model_sample_count;
                probability_input.model_sample_span_seconds = asset->model_sample_span_seconds;
                probability_input.momentum_bps_30s = asset->momentum_bps_30s;
            }
            probability_input.paired_book_imbalance = paired_imbalance;
            const auto directional_probability = strategy::probability_model(probability_input, strategy_config_);
            const auto lottery_probability = strategy::lottery_probability_model(probability_input, strategy_config_);
            std::string probability_block_reason;
            if (!directional_probability.estimated_probability || !lottery_probability.estimated_probability) {
                if (!opening_price) probability_block_reason = market.start_ts <= 0
                    ? "price_to_beat_start_time_unavailable"
                    : timestamp * 1000 > market.start_ts * 1000 + 10000
                        ? "price_to_beat_capture_missed" : "price_to_beat_pending";
                else if (!reference.settlement_reference) probability_block_reason = "settlement_reference_unavailable";
                else if (!asset || !asset->volatility_per_sqrt_second) probability_block_reason = "volatility_unavailable";
                else if (asset->model_sample_count < 20) probability_block_reason = "insufficient_model_samples";
                else if (asset->model_sample_span_seconds < strategy_config_.minimum_model_sample_span_seconds)
                    probability_block_reason = "model_sample_span_insufficient";
                else if (!asset->momentum_bps_30s) probability_block_reason = "momentum_unavailable";
                else probability_block_reason = "probability_model_unavailable";
            }
            const auto up = buy_vwap(up_book, size_);
            const auto down = buy_vwap(down_book, size_);
            const double rate = market.fee > 0 ? market.fee : fallback_fee_;
            std::map<std::string, strategy::EvaluationInput> directional_inputs;
            std::map<std::string, strategy::Decision> directional_decisions;
            std::map<std::string, strategy::EvaluationInput> lottery_inputs;
            for (const std::string outcome : {"Up", "Down"}) {
                const bool is_up = outcome == "Up";
                const Book& book = is_up ? up_book : down_book;
                const auto fill = is_up ? up : down;
                const double ask = best_ask(book);
                const double fee = std::round(fill.first * rate * fill.second * (1 - fill.second) * 100000) / 100000;
                const double slippage = ask > 0 ? std::max(0.0, fill.second - ask) : 1e9;
                const std::optional<double> directional_raw_probability = !directional_probability.estimated_probability
                    ? std::nullopt
                    : is_up ? directional_probability.estimated_probability
                            : std::optional<double>(1 - *directional_probability.estimated_probability);
                const std::optional<double> lottery_raw_probability = !lottery_probability.estimated_probability
                    ? std::nullopt
                    : is_up ? lottery_probability.estimated_probability
                            : std::optional<double>(1 - *lottery_probability.estimated_probability);
                strategy::EvaluationInput input;
                input.timeframe = market.interval;
                input.expected_fill_price = fill.second;
                input.seconds_to_close = seconds_to_close;
                input.price_to_beat = opening_price;
                input.fee_per_share = fee / std::max(size_, 1e-9);
                input.slippage_per_share = slippage;
                input.liquidity = available_ask_depth(book);
                input.book_age_ms = book_age_ms;
                input.reference_age_ms = reference.reference_age_ms;
                input.clock_skew_ms = reference.clock_skew_ms;
                input.market_active = market.close_ts > timestamp;
                input.market_tradable = market.accepting_orders;
                input.target_depth_ok = fill.first >= size_;
                input.momentum_bps_30s = asset ? asset->momentum_bps_30s : std::nullopt;
                input.order_book_imbalance = book_imbalance(book);
                input.reference_quorum_met = reference.quorum;
                input.reference_block_reason = reference.reason;
                input.settlement_source_verified = reference.settlement_verified;
                input.probability_block_reason = probability_block_reason;
                input.minimum_liquidity = strategy_config_.minimum_liquidity;
                input.maximum_slippage = strategy_config_.maximum_slippage;
                input.maximum_reference_age_ms = reference.maximum_reference_age_ms;
                input.maximum_book_age_ms = strategy_config_.maximum_book_age_ms;
                input.maximum_clock_skew_ms = strategy_config_.maximum_clock_skew_ms;
                for (const std::string strategy_name : {"late_window_directional_ev", "low_price_lottery_ev"}) {
                    input.strategy = strategy_name;
                    const bool is_lottery = strategy_name == "low_price_lottery_ev";
                    const auto raw_probability = is_lottery
                        ? lottery_raw_probability : directional_raw_probability;
                    input.estimated_probability = is_lottery
                        ? strategy::lottery_market_blend_probability(raw_probability, ask, strategy_config_)
                        : raw_probability;
                    strategy::Decision decision = strategy_name == "late_window_directional_ev"
                        ? strategy::evaluate_directional(input, strategy_config_)
                        : strategy::evaluate_lottery(input, strategy_config_);
                    if (is_lottery) lottery_inputs[outcome] = input;
                    else {
                        directional_inputs[outcome] = input;
                        directional_decisions[outcome] = decision;
                    }
                    if (!strategy_audit_.available()) {
                        decision.decision = "REJECT";
                        decision.reason = "audit_backpressure";
                        decision.blocking_reasons.insert(decision.blocking_reasons.begin(), "audit_backpressure");
                    }
                    const std::string key = item.first + "|" + strategy_name + "|" + outcome;
                    if (!should_emit_strategy(key, decision, timestamp)) continue;
                    emit_strategy_audit(item.first, market, outcome, input, probability_input,
                                        is_lottery ? lottery_probability : directional_probability,
                                        raw_probability, reference, decision, timestamp, ask);
                }
            }
            emit_terminal_hedge_evaluation(
                item.first, market, directional_inputs, directional_decisions,
                lottery_inputs, probability_input, timestamp);
            emit_complete_set_evaluations(
                item.first, market, up_book, down_book, directional_inputs,
                directional_probability.estimated_probability, opening_price, timestamp);
        }
        const auto evaluation_finished = std::chrono::steady_clock::now();
        if (evaluated_any && last_clob_mutation_at_.time_since_epoch().count()) {
            clob_to_strategy_evaluation_us_.add(std::chrono::duration<double, std::micro>(
                evaluation_finished - last_clob_mutation_at_).count());
        }
        last_clob_mutation_at_ = {};
    }

    void emit_terminal_hedge_evaluation(
            const std::string& market_id, const Market& market,
            const std::map<std::string, strategy::EvaluationInput>& directional_inputs,
            const std::map<std::string, strategy::Decision>& directional_decisions,
            const std::map<std::string, strategy::EvaluationInput>& lottery_inputs,
            const strategy::ProbabilityInput& probability_input,
            double timestamp) {
        if (directional_inputs.empty()) return;
        const int seconds_to_close = directional_inputs.begin()->second.seconds_to_close;
        const auto terminal_window = strategy::directional_window(market.interval, strategy_config_);
        if (!terminal_window || seconds_to_close < terminal_window->first ||
            seconds_to_close > terminal_window->second) return;
        const strategy::Decision* main_decision = nullptr;
        const strategy::EvaluationInput* main = nullptr;
        std::string main_outcome;
        const strategy::Decision* diagnostic_decision = nullptr;
        const strategy::EvaluationInput* diagnostic = nullptr;
        std::string diagnostic_outcome;
        for (const std::string outcome : {"Up", "Down"}) {
            const auto decision = directional_decisions.find(outcome);
            const auto input = directional_inputs.find(outcome);
            if (decision == directional_decisions.end() || input == directional_inputs.end()) continue;
            if (!diagnostic || input->second.estimated_probability.value_or(-1) >
                diagnostic->estimated_probability.value_or(-1)) {
                diagnostic_decision = &decision->second;
                diagnostic = &input->second;
                diagnostic_outcome = outcome;
            }
            if (decision->second.decision != "ACCEPT") continue;
            if (!main_decision || decision->second.net_ev.value_or(-1e9) > main_decision->net_ev.value_or(-1e9)) {
                main_decision = &decision->second;
                main = &input->second;
                main_outcome = outcome;
            }
        }
        if (!main) {
            main_decision = diagnostic_decision;
            main = diagnostic;
            main_outcome = diagnostic_outcome;
        }
        std::string reason = main_decision ? main_decision->reason : "directional_not_evaluated";
        bool accepted = false;
        strategy::TerminalHedgeOutput result;
        std::string hedge_outcome;
        const strategy::EvaluationInput* hedge = nullptr;
        if (main) {
            hedge_outcome = main_outcome == "Up" ? "Down" : "Up";
            const auto found = lottery_inputs.find(hedge_outcome);
            if (found != lottery_inputs.end()) hedge = &found->second;
        }
        if (main && main_decision && main_decision->decision == "ACCEPT" &&
            main->estimated_probability) {
            if (!hedge) reason = "hedge_quote_unavailable";
            else {
                result = strategy::evaluate_terminal_hedge({
                    size_, *main->estimated_probability,
                    main->expected_fill_price, main->fee_per_share, main->slippage_per_share,
                    hedge->expected_fill_price, hedge->fee_per_share, hedge->slippage_per_share,
                    hedge->liquidity, hedge->minimum_liquidity, hedge->maximum_slippage,
                    hedge->target_depth_ok,
                }, strategy_config_);
                reason = result.reason;
                accepted = result.accepted;
            }
        }
        strategy::Decision combined{
            "late_window_directional_ev", std::nullopt,
            accepted ? std::optional<double>(result.expected_pnl / std::max(size_, 1e-9)) : std::nullopt,
            accepted ? "ACCEPT" : "REJECT", reason, {reason},
        };
        const std::string state_key = market_id + "|terminal_hedge";
        if (!should_emit_strategy(state_key, combined, timestamp)) return;
        const unsigned long long sequence = ++strategy_evaluation_sequence_;
        const std::string event_id = run_id_ + ":" + std::to_string(generation_) + ":" +
            std::to_string(ws_session_id_) + ":" + market_id + ":terminal_hedge:" +
            std::to_string(sequence);
        std::ostringstream out;
        const bool main_fill_available = main && main->target_depth_ok;
        const bool hedge_fill_available = hedge && hedge->target_depth_ok;
        const bool helper_evaluated = main && main_decision &&
            main_decision->decision == "ACCEPT" && main->estimated_probability && hedge;
        const bool full_cost_chain = helper_evaluated && result.total_cost > 0;
        const auto optional = [&](const std::optional<double>& value) {
            if (value && std::isfinite(*value)) out << *value;
            else out << "null";
        };
        const auto available = [&](bool present, double value) {
            if (present && std::isfinite(value)) out << value;
            else out << "null";
        };
        out << std::setprecision(15)
            << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
            << ",\"event_id\":\"" << reference_ipc::escaped(event_id)
            << "\",\"event_type\":\"" << (accepted ? "shadow_hedged_opportunity" : "shadow_hedge_eval")
            << "\",\"strategy\":\"late_window_directional_ev\",\"hedge_strategy\":\"low_price_lottery_ev\""
            << ",\"execution_mode\":\"terminal_hedged\",\"market_id\":\""
            << reference_ipc::escaped(market_id) << "\",\"condition_id\":\""
            << reference_ipc::escaped(market.condition_id) << "\",\"asset\":\""
            << reference_ipc::escaped(market.asset) << "\",\"timeframe\":\""
            << reference_ipc::escaped(market.interval) << "\",\"window\":\""
            << reference_ipc::escaped(market.window) << "\",\"generation\":" << generation_
            << ",\"session\":" << ws_session_id_ << ",\"evaluation_sequence\":" << sequence
            << ",\"main_outcome\":\"" << main_outcome << "\",\"hedge_outcome\":\""
            << hedge_outcome << "\",\"main_size\":" << size_ << ",\"hedge_size\":";
        available(result.hedge_size > 0, result.hedge_size);
        out << ",\"main_expected_fill_price\":";
        available(main_fill_available, main ? main->expected_fill_price : 0);
        out << ",\"hedge_expected_fill_price\":";
        available(hedge_fill_available, hedge ? hedge->expected_fill_price : 0);
        out << ",\"main_probability\":";
        available(helper_evaluated, result.main_probability);
        out << ",\"hedge_probability\":";
        available(helper_evaluated, result.hedge_probability);
        out << ",\"main_unit_cost\":";
        available(helper_evaluated, result.main_unit_cost);
        out << ",\"hedge_unit_cost\":";
        available(helper_evaluated, result.hedge_unit_cost);
        out << ",\"main_net_ev_per_share\":";
        available(helper_evaluated, result.main_net_ev_per_share);
        out << ",\"hedge_net_ev_per_share\":";
        available(helper_evaluated, result.hedge_net_ev_per_share);
        out << ",\"main_cost\":";
        available(helper_evaluated, result.main_cost);
        out << ",\"hedge_cost\":";
        available(result.hedge_cost > 0, result.hedge_cost);
        out << ",\"total_cost\":";
        available(full_cost_chain, result.total_cost);
        out << ",\"main_win_pnl\":";
        available(full_cost_chain, result.main_win_pnl);
        out << ",\"reversal_pnl\":";
        available(full_cost_chain, result.reversal_pnl);
        out << ",\"expected_portfolio_pnl\":";
        available(full_cost_chain, result.expected_pnl);
        out << ",\"worst_case_pnl\":";
        available(full_cost_chain, std::min(result.main_win_pnl, result.reversal_pnl));
        out << ",\"estimated_probability\":";
        optional(main ? main->estimated_probability : std::nullopt);
        out << ",\"volatility_per_sqrt_second\":";
        optional(probability_input.volatility_per_sqrt_second);
        out << ",\"model_sample_count\":" << probability_input.model_sample_count
            << ",\"model_sample_span_seconds\":" << probability_input.model_sample_span_seconds
            << ",\"settlement_reference\":";
        optional(probability_input.settlement_reference);
        out << ",\"price_to_beat\":";
        optional(probability_input.price_to_beat);
        out << ",\"reference_quorum_met\":"
            << (main && main->reference_quorum_met ? "true" : "false")
            << ",\"seconds_to_close\":" << (main ? main->seconds_to_close : 0)
            << ",\"decision\":\"" << combined.decision << "\",\"reason\":\"" << reason
            << "\",\"target_size\":" << size_ << ",\"config_version\":\"terminal-hedge-v1\""
            << ",\"config_hash\":\"" << strategy_config_hash() << "\""
            << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
    }

    void emit_complete_set_evaluations(
            const std::string& market_id, const Market& market,
            const Book& up_book, const Book& down_book,
            const std::map<std::string, strategy::EvaluationInput>& inputs,
            const std::optional<double>& up_probability,
            const std::optional<double>& price_to_beat, double timestamp) {
        const auto up_found = inputs.find("Up");
        const auto down_found = inputs.find("Down");
        if (up_found == inputs.end() || down_found == inputs.end()) return;
        const auto& up = up_found->second;
        const auto& down = down_found->second;
        auto& inventory = complete_set_inventory_[market_id];
        double total_unmatched_notional = 0;
        for (const auto& item : complete_set_inventory_)
            total_unmatched_notional += item.second.up_cost + item.second.down_cost;
        const double other_inventory_notional = std::max(
            0.0, total_unmatched_notional - inventory.up_cost - inventory.down_cost);
        const double available_global_notional = std::max(
            0.0, inventory_max_total_unmatched_notional_ - other_inventory_notional);
        const double available_market_notional = std::min(
            inventory_max_unmatched_notional_, available_global_notional);

        complete_set::RebalanceDecision rebalance;
        if (!up_probability) {
            rebalance.reason = "probability_model_unavailable";
        } else if (!up.market_active || !up.market_tradable) {
            rebalance.reason = "market_not_tradable";
        } else if (!up.reference_quorum_met) {
            rebalance.reason = up.reference_block_reason.empty()
                ? "insufficient_reference_sources" : up.reference_block_reason;
        } else if (!up.settlement_source_verified) {
            rebalance.reason = "settlement_reference_unverified";
        } else if (!up.clock_skew_ms ||
                   std::abs(*up.clock_skew_ms) > up.maximum_clock_skew_ms) {
            rebalance.reason = up.clock_skew_ms
                ? "clock_skew_exceeded" : "clock_skew_unavailable";
        } else if (up.book_age_ms > up.maximum_book_age_ms ||
                   down.book_age_ms > down.maximum_book_age_ms) {
            rebalance.reason = "clob_book_stale";
        } else {
            rebalance = complete_set::evaluate_rebalance({
                inventory, size_, *up_probability,
                up.expected_fill_price + up.fee_per_share + buffer_per_share_,
                down.expected_fill_price + down.fee_per_share + buffer_per_share_,
                up.liquidity, down.liquidity,
                inventory_min_entry_edge_, inventory_min_entry_ev_roi_,
                inventory_max_initial_price_, inventory_max_complement_gap_,
                min_profit_, inventory_min_locked_roi_, available_market_notional,
            });
        }

        double locked_profit = 0;
        const bool inventory_was_empty =
            inventory.up_quantity <= 1e-12 && inventory.down_quantity <= 1e-12;
        if (rebalance.decision == "ACCEPT") {
            if (inventory_was_empty)
                inventory_origin_config_hashes_[market_id] = inventory_strategy_config_hash_;
            if (rebalance.action == "BUY_UP" || rebalance.action == "BUY_UP_AND_LOCK") {
                inventory.up_quantity += rebalance.quantity;
                inventory.up_cost += rebalance.quantity * rebalance.unit_cost;
            } else {
                inventory.down_quantity += rebalance.quantity;
                inventory.down_cost += rebalance.quantity * rebalance.unit_cost;
            }
            if (rebalance.projected_locked_quantity > 0) {
                const double locked = std::min({
                    rebalance.projected_locked_quantity,
                    inventory.up_quantity,
                    inventory.down_quantity,
                });
                const double up_average = inventory.up_cost /
                    std::max(inventory.up_quantity, 1e-12);
                const double down_average = inventory.down_cost /
                    std::max(inventory.down_quantity, 1e-12);
                locked_profit = locked * (1 - up_average - down_average);
                inventory.up_quantity -= locked;
                inventory.down_quantity -= locked;
                inventory.up_cost = std::max(0.0, inventory.up_cost - locked * up_average);
                inventory.down_cost = std::max(0.0, inventory.down_cost - locked * down_average);
            }
            save_complete_set_inventory();
        }

        strategy::Decision inventory_audit{
            "inventory_rebalancing_arb", std::nullopt, std::nullopt,
            rebalance.decision, rebalance.reason, {rebalance.reason},
        };
        const std::string inventory_key = market_id + "|inventory_rebalancing_arb";
        if (should_emit_strategy(inventory_key, inventory_audit, timestamp)) {
            const unsigned long long sequence = ++strategy_evaluation_sequence_;
            std::ostringstream out;
            out << std::setprecision(15)
                << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
                << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':'
                << ws_session_id_ << ':' << market_id << ":inventory:" << sequence
                << "\",\"event_type\":\""
                << (rebalance.decision == "ACCEPT"
                    ? "shadow_inventory_action" : "shadow_inventory_eval")
                << "\",\"strategy\":\"inventory_rebalancing_arb\",\"market_id\":\""
                << reference_ipc::escaped(market_id) << "\",\"condition_id\":\""
                << reference_ipc::escaped(market.condition_id) << "\",\"asset\":\""
                << reference_ipc::escaped(market.asset) << "\",\"timeframe\":\""
                << reference_ipc::escaped(market.interval) << "\",\"window\":\""
                << reference_ipc::escaped(market.window) << "\",\"generation\":"
                << generation_ << ",\"session\":" << ws_session_id_
                << ",\"close_ts\":" << market.close_ts
                << ",\"settlement_source\":\""
                << reference_ipc::escaped(market.settlement_source)
                << "\",\"price_to_beat\":";
            if (price_to_beat) out << *price_to_beat;
            else out << "null";
            out
                << ",\"evaluation_sequence\":" << sequence << ",\"action\":\""
                << rebalance.action << "\",\"outcome\":\"" << rebalance.outcome
                << "\",\"quantity\":" << rebalance.quantity
                << ",\"unit_cost\":" << rebalance.unit_cost
                << ",\"estimated_probability\":" << rebalance.probability
                << ",\"probability_edge\":" << rebalance.probability_edge
                << ",\"expected_value\":" << rebalance.expected_value
                << ",\"expected_value_roi\":" << rebalance.expected_value_roi
                << ",\"maximum_loss\":" << rebalance.maximum_loss
                << ",\"complement_gap\":" << rebalance.complement_gap
                << ",\"projected_locked_quantity\":" << rebalance.projected_locked_quantity
                << ",\"projected_locked_profit\":" << rebalance.projected_locked_profit
                << ",\"projected_locked_roi\":" << rebalance.projected_locked_roi
                << ",\"realized_locked_profit\":" << locked_profit
                << ",\"residual_up_quantity\":" << inventory.up_quantity
                << ",\"residual_down_quantity\":" << inventory.down_quantity
                << ",\"residual_up_cost\":" << inventory.up_cost
                << ",\"residual_down_cost\":" << inventory.down_cost
                << ",\"total_unmatched_notional\":" << total_unmatched_notional
                << ",\"available_global_unmatched_notional\":" << available_global_notional
                << ",\"inventory_origin_config_hash\":\""
                << reference_ipc::escaped(
                    inventory_origin_config_hashes_.count(market_id)
                        ? inventory_origin_config_hashes_.at(market_id)
                        : inventory_strategy_config_hash_)
                << "\",\"decision\":\"" << rebalance.decision << "\",\"reason\":\""
                << rebalance.reason << "\",\"config_version\":\"inventory-rebalancing-v1\""
                << ",\"config_hash\":\"" << inventory_strategy_config_hash_ << "\""
                << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
            if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
        }

        complete_set::MakerDecision maker;
        if (!up_probability) {
            maker.reason = "probability_model_unavailable";
        } else if (!up.market_active || !up.market_tradable) {
            maker.reason = "market_not_tradable";
        } else if (!up.reference_quorum_met) {
            maker.reason = up.reference_block_reason.empty()
                ? "insufficient_reference_sources" : up.reference_block_reason;
        } else if (!up.settlement_source_verified) {
            maker.reason = "settlement_reference_unverified";
        } else if (!up.clock_skew_ms ||
                   std::abs(*up.clock_skew_ms) > up.maximum_clock_skew_ms) {
            maker.reason = up.clock_skew_ms
                ? "clock_skew_exceeded" : "clock_skew_unavailable";
        } else if (up.book_age_ms > up.maximum_book_age_ms ||
                   down.book_age_ms > down.maximum_book_age_ms) {
            maker.reason = "clob_book_stale";
        } else if (maker_both_fill_probability_ <= 0) {
            maker.reason = "maker_fill_probability_unavailable";
        } else {
            const double inventory_skew = (
                inventory.up_quantity - inventory.down_quantity
            ) / std::max(size_, 1e-9) * maker_inventory_skew_per_unit_;
            maker = complete_set::evaluate_maker({
                *up_probability, best_bid(up_book), best_ask(up_book),
                best_bid(down_book), best_ask(down_book), maker_tick_size_,
                maker_quote_half_spread_, inventory_skew,
                maker_expected_rebate_per_pair_, maker_minimum_pair_edge_,
                maker_both_fill_probability_, maker_orphan_loss_,
            });
        }
        strategy::Decision maker_audit{
            "maker_complete_set_arb", std::nullopt, std::nullopt,
            maker.decision, maker.reason, {maker.reason},
        };
        const std::string maker_key = market_id + "|maker_complete_set_arb";
        if (should_emit_strategy(maker_key, maker_audit, timestamp)) {
            const unsigned long long sequence = ++strategy_evaluation_sequence_;
            std::ostringstream out;
            out << std::setprecision(15)
                << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
                << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':'
                << ws_session_id_ << ':' << market_id << ":maker:" << sequence
                << "\",\"event_type\":\"shadow_maker_quote_eval\""
                << ",\"strategy\":\"maker_complete_set_arb\",\"market_id\":\""
                << reference_ipc::escaped(market_id) << "\",\"condition_id\":\""
                << reference_ipc::escaped(market.condition_id) << "\",\"asset\":\""
                << reference_ipc::escaped(market.asset) << "\",\"timeframe\":\""
                << reference_ipc::escaped(market.interval) << "\",\"window\":\""
                << reference_ipc::escaped(market.window) << "\",\"generation\":"
                << generation_ << ",\"session\":" << ws_session_id_
                << ",\"close_ts\":" << market.close_ts
                << ",\"evaluation_sequence\":" << sequence
                << ",\"up_bid_quote\":" << maker.up_bid
                << ",\"down_bid_quote\":" << maker.down_bid
                << ",\"pair_quote_cost\":" << maker.pair_cost
                << ",\"locked_edge_if_both_fill\":" << maker.locked_edge
                << ",\"configured_both_fill_probability\":"
                << maker_both_fill_probability_
                << ",\"expected_rebate_per_pair\":" << maker_expected_rebate_per_pair_
                << ",\"expected_value\":" << maker.expected_value
                << ",\"fill_model\":\"configured_unverified\""
                << ",\"decision\":\"" << maker.decision << "\",\"reason\":\""
                << maker.reason << "\",\"config_version\":\"maker-complete-set-v1\""
                << ",\"config_hash\":\"" << maker_strategy_config_hash_ << "\""
                << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
            if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
        }
    }

    void emit_strategy_audit(const std::string& market_id, const Market& market,
                             const std::string& outcome, const strategy::EvaluationInput& input,
                             const strategy::ProbabilityInput& model_input,
                             const strategy::ProbabilityOutput& model,
                             const std::optional<double>& raw_estimated_probability,
                             const ReferenceView& reference, const strategy::Decision& decision,
                             double timestamp, double market_price) {
        const unsigned long long sequence = ++strategy_evaluation_sequence_;
        const std::string event_id = run_id_ + ":" + std::to_string(generation_) + ":" +
            std::to_string(ws_session_id_) + ":" + market_id + ":" + decision.strategy + ":" +
            outcome + ":" + std::to_string(sequence);
        std::ostringstream out;
        out << std::setprecision(15);
        const auto optional = [&](const std::optional<double>& value) {
            if (value && std::isfinite(*value)) out << *value;
            else out << "null";
        };
        out << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
            << ",\"event_id\":\"" << reference_ipc::escaped(event_id)
            << "\",\"event_type\":\"shadow_eval\",\"strategy\":\"" << decision.strategy
            << "\",\"market_id\":\"" << reference_ipc::escaped(market_id)
            << "\",\"condition_id\":\"" << reference_ipc::escaped(market.condition_id)
            << "\",\"asset\":\"" << reference_ipc::escaped(market.asset)
            << "\",\"timeframe\":\"" << reference_ipc::escaped(market.interval)
            << "\",\"window\":\"" << reference_ipc::escaped(market.window)
            << "\",\"generation\":" << generation_ << ",\"session\":" << ws_session_id_
            << ",\"evaluation_sequence\":" << sequence << ",\"outcome\":\"" << outcome
            << "\",\"market_price\":" << market_price << ",\"expected_fill_price\":"
            << input.expected_fill_price << ",\"estimated_probability\":";
        optional(input.estimated_probability);
        out << ",\"raw_estimated_probability\":"; optional(raw_estimated_probability);
        out << ",\"market_implied_probability\":" << market_price << ",\"gross_edge\":";
        optional(decision.gross_edge);
        out << ",\"fees\":" << input.fee_per_share << ",\"slippage\":" << input.slippage_per_share
            << ",\"latency_risk_buffer\":" << strategy_config_.directional_latency_buffer
            << ",\"settlement_risk_buffer\":" << strategy_config_.directional_settlement_buffer
            << ",\"model_uncertainty_buffer\":" << strategy_config_.lottery_model_buffer
            << ",\"execution_risk_buffer\":" << strategy_config_.lottery_execution_buffer
            << ",\"net_ev\":";
        optional(decision.net_ev);
        out << ",\"fast_price\":"; optional(reference.fast_price);
        out << ",\"consensus_price\":"; optional(reference.consensus_price);
        out << ",\"settlement_reference\":"; optional(reference.settlement_reference);
        out << ",\"fresh_exchange_source_count\":" << reference.fresh_exchange_sources
            << ",\"fresh_usd_spot_source_count\":" << reference.fresh_usd_sources
            << ",\"cross_source_divergence_bps\":";
        optional(reference.divergence_bps);
        out << ",\"reference_quorum_met\":" << (reference.quorum ? "true" : "false")
            << ",\"reference_state\":\"" << reference.state << "\",\"reference_block_reason\":";
        if (reference.reason.empty()) out << "null";
        else out << '"' << reference_ipc::escaped(reference.reason) << '"';
        out << ",\"reference_source_statuses\":[";
        bool first_source = true;
        for (const auto& source : reference.sources) {
            if (!first_source) out << ',';
            first_source = false;
            out << "{\"source\":\"" << reference_ipc::escaped(source.source)
                << "\",\"symbol\":\"" << reference_ipc::escaped(source.symbol)
                << "\",\"market_type\":\"" << reference_ipc::escaped(source.market_type)
                << "\",\"quote_currency\":\"" << reference_ipc::escaped(source.quote_currency)
                << "\",\"price\":"; optional(source.price);
            out << ",\"effective_age_ms\":"; optional(source.age_ms);
            out << ",\"status\":\"" << source.status << "\"}";
        }
        out << "],\"reference_price\":"; optional(reference.settlement_reference);
        out << ",\"probability_reference_source\":\"settlement_reference\""
            << ",\"probability_reference_price\":";
        optional(reference.settlement_reference);
        out << ",\"price_to_beat\":"; optional(input.price_to_beat);
        out << ",\"price_to_beat_source\":";
        if (!market.open_price_source.empty())
            out << '"' << reference_ipc::escaped(market.open_price_source) << '"';
        else out << "null";
        out << ",\"price_to_beat_capture_mode\":";
        if (!market.open_price_capture_mode.empty())
            out << '"' << reference_ipc::escaped(market.open_price_capture_mode) << '"';
        else out << "null";
        out << ",\"price_to_beat_source_timestamp_ms\":";
        optional(market.open_price_source_timestamp_ms);
        out << ",\"distance_to_price_to_beat\":";
        if (reference.settlement_reference && input.price_to_beat)
            out << *reference.settlement_reference - *input.price_to_beat;
        else out << "null";
        out << ",\"seconds_to_close\":" << input.seconds_to_close
            << ",\"book_age_ms\":" << input.book_age_ms << ",\"reference_age_ms\":";
        optional(input.reference_age_ms);
        out << ",\"clock_skew_ms\":"; optional(input.clock_skew_ms);
        out << ",\"liquidity\":" << input.liquidity
            << ",\"minimum_liquidity\":" << input.minimum_liquidity
            << ",\"target_depth_ok\":" << (input.target_depth_ok ? "true" : "false")
            << ",\"maximum_slippage\":" << input.maximum_slippage
            << ",\"maximum_reference_age_ms\":" << input.maximum_reference_age_ms
            << ",\"maximum_book_age_ms\":" << input.maximum_book_age_ms
            << ",\"maximum_clock_skew_ms\":" << input.maximum_clock_skew_ms
            << ",\"momentum_bps_30s\":"; optional(input.momentum_bps_30s);
        out << ",\"order_book_imbalance\":"; optional(input.order_book_imbalance);
        out << ",\"paired_book_imbalance\":"; optional(model_input.paired_book_imbalance);
        out << ",\"volatility_per_sqrt_second\":"; optional(model_input.volatility_per_sqrt_second);
        out << ",\"model_sample_count\":" << model_input.model_sample_count
            << ",\"model_sample_span_seconds\":" << model_input.model_sample_span_seconds
            << ",\"minimum_model_sample_span_seconds\":" << strategy_config_.minimum_model_sample_span_seconds
            << ",\"expected_move_log_std\":"; optional(model.expected_move_log_std);
        out << ",\"reference_log_distance\":"; optional(model.reference_log_distance);
        out << ",\"up_standardized_distance\":"; optional(model.up_standardized_distance);
        out << ",\"up_momentum_z\":"; optional(model.up_momentum_z);
        out << ",\"up_imbalance_z\":"; optional(model.up_imbalance_z);
        out << ",\"up_final_model_z\":"; optional(model.up_final_model_z);
        const double confidence = input.estimated_probability
            ? std::clamp(std::min(model_input.model_sample_count / 120.0, 1.0) *
                (1 - std::min(reference.divergence_bps.value_or(0) / 100, 1.0)), 0.0, 1.0)
            : 0;
        out << ",\"confidence\":";
        if (input.estimated_probability) out << confidence; else out << "null";
        out << ",\"input_quality_score\":";
        if (input.estimated_probability) out << confidence; else out << "null";
        const bool is_lottery = decision.strategy == "low_price_lottery_ev";
        out << ",\"confidence_type\":\"input_quality_not_historical_accuracy\""
            << ",\"probability_model_id\":\""
            << (is_lottery ? "lottery_market_blend_v1" : "directional_normal_cdf_v1") << '"'
            << ",\"model_type\":\""
            << (is_lottery ? "configured_lottery_market_blend_shadow" : "configured_distributional_shadow")
            << "\",\"model_source\":";
        if (input.estimated_probability) out << "\"live_multi_source\""; else out << "null";
        out << ",\"settlement_source\":\"" << reference_ipc::escaped(market.settlement_source)
            << "\",\"settlement_source_verified\":" << (input.settlement_source_verified ? "true" : "false")
            << ",\"market_active\":" << (input.market_active ? "true" : "false")
            << ",\"market_tradable\":" << (input.market_tradable ? "true" : "false")
            << ",\"probability_block_reason\":";
        if (input.probability_block_reason.empty()) out << "null";
        else out << '"' << reference_ipc::escaped(input.probability_block_reason) << '"';
        out
            << ",\"decision\":\"" << decision.decision << "\",\"reason\":\"" << decision.reason
            << "\",\"blocking_reasons\":[";
        for (std::size_t index = 0; index < decision.blocking_reasons.size(); ++index) {
            if (index) out << ',';
            out << '"' << reference_ipc::escaped(decision.blocking_reasons[index]) << '"';
        }
        out << "],\"target_size\":" << size_ << ",\"config_version\":\"shadow-buy-rules-v7\""
            << ",\"config_hash\":\"" << strategy_hash_for(decision.strategy) << "\""
            << ",\"reference_sequence\":" << reference_snapshot_.sequence
            << ",\"reference_producer_session\":\"" << reference_ipc::escaped(reference_snapshot_.producer_session) << "\""
            << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
    }

    void load_complete_set_inventory() {
        try {
            if (!std::filesystem::exists(inventory_state_path_)) return;
            ptree root;
            boost::property_tree::read_json(inventory_state_path_, root);
            const std::string state_config_hash = root.get<std::string>("config_hash", "");
            if (const auto markets = root.get_child_optional("markets")) {
                for (const auto& item : *markets) {
                    complete_set_inventory_[item.first] = {
                        item.second.get<double>("up_quantity", 0),
                        item.second.get<double>("down_quantity", 0),
                        item.second.get<double>("up_cost", 0),
                        item.second.get<double>("down_cost", 0),
                    };
                    inventory_origin_config_hashes_[item.first] =
                        item.second.get<std::string>(
                            "origin_config_hash", state_config_hash);
                }
            }
        } catch (const std::exception& error) {
            std::cerr << "INVENTORY_STATE_LOAD_ERROR message=" << error.what() << "\n";
            complete_set_inventory_.clear();
        }
    }

    void save_complete_set_inventory() {
        try {
            const std::filesystem::path path(inventory_state_path_);
            if (path.has_parent_path()) std::filesystem::create_directories(path.parent_path());
            ptree root, rows;
            root.put("config_hash", inventory_strategy_config_hash_);
            root.put("updated_at", now_seconds());
            for (const auto& item : complete_set_inventory_) {
                ptree row;
                row.put("up_quantity", item.second.up_quantity);
                row.put("down_quantity", item.second.down_quantity);
                row.put("up_cost", item.second.up_cost);
                row.put("down_cost", item.second.down_cost);
                row.put(
                    "origin_config_hash",
                    inventory_origin_config_hashes_.count(item.first)
                        ? inventory_origin_config_hashes_.at(item.first)
                        : inventory_strategy_config_hash_);
                rows.add_child(item.first, row);
            }
            root.add_child("markets", rows);
            const std::filesystem::path temporary = path.string() + ".tmp";
            boost::property_tree::write_json(
                temporary.string(), root, std::locale(), false);
            std::error_code error;
            std::filesystem::rename(temporary, path, error);
            if (error) {
                std::filesystem::remove(path, error);
                error.clear();
                std::filesystem::rename(temporary, path, error);
            }
            if (error) throw std::runtime_error(error.message());
        } catch (const std::exception& error) {
            std::cerr << "INVENTORY_STATE_SAVE_ERROR message=" << error.what() << "\n";
        }
    }

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
        schedule_evaluation();
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
            ++books_[asset].version;
            last_clob_mutation_at_ = std::chrono::steady_clock::now();
            ++book_events_;
        } else if (type == "price_change") {
            const double source_timestamp = message.get<double>("timestamp", 0);
            auto changes = message.get_child_optional("price_changes");
            if (!changes) return;
            bool changed = false;
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
                ++books_[token].version;
                changed = true;
                if (crossed(books_[token])) resync_token(token, "crossed_book");
            }
            if (changed) last_clob_mutation_at_ = std::chrono::steady_clock::now();
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
            const double up_age_ms = std::max(0.0, (timestamp - up_book.updated_at) * 1000);
            const double down_age_ms = std::max(0.0, (timestamp - down_book.updated_at) * 1000);
            const double book_state_age_ms = std::max(up_age_ms, down_age_ms);
            const double clob_feed_age_ms = std::max(0.0, (timestamp - last_activity_) * 1000);
            const double effective_book_age_ms = std::min(book_state_age_ms, clob_feed_age_ms);
            const bool books_synced = effective_book_age_ms <= 750;
            const double seconds_to_close = item.second.close_ts - timestamp;
            const double source_timestamp_age_ms = std::max(
                std::abs(timestamp * 1000 - up_book.source_timestamp_ms),
                std::abs(timestamp * 1000 - down_book.source_timestamp_ms));
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
            const std::string reason = !books_synced ? "clob_book_stale" : seconds_to_close < 20 ? "closing_window" : seconds_to_close > 7200 ? "too_early" : up.first < size_ ? "up_depth" : down.first < size_ ? "down_depth" : profit < min_profit_ ? "net_cost_above_threshold" : expected_execution_value < min_expected_value_ ? "execution_value_below_threshold" : "opportunity";
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
                                   << ",\"clock_skew_ms\":" << source_timestamp_age_ms
                                   << ",\"clock_skew_basis\":\"clob_source_timestamp_age_diagnostic\",\"source_age_ms\":" << source_timestamp_age_ms
                                   << ",\"source_timestamp_age_ms\":" << source_timestamp_age_ms
                                   << ",\"book_age_ms\":" << effective_book_age_ms
                                   << ",\"book_age_basis\":\"min(book_state_age_ms,clob_feed_age_ms)\""
                                   << ",\"book_state_age_ms\":" << book_state_age_ms << ",\"clob_feed_age_ms\":" << clob_feed_age_ms
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
                                   << ",\"books_synced\":" << (books_synced ? "true" : "false")
                                   << ",\"config_version\":\"paired-lock-shadow-v2\",\"config_hash\":\""
                                   << paired_config_hash_ << "\",\"decision\":\""
                                   << (good ? "ACCEPT" : "REJECT") << "\"}\n" << std::flush;
                item.second.last_reason = reason;
                item.second.last_audit = timestamp;
            }
            if (good) std::cout << "SHADOW_OPPORTUNITY\tmarket=" << item.first << "\tup_vwap=" << std::setprecision(12) << up.second
                                << "\tdown_vwap=" << down.second << "\tfees=" << up_fee + down_fee << "\tnet_cost=" << net_cost
                                << "\tprofit=" << profit << "\tfok=1\tduration_ms=" << (timestamp - item.second.active_since) * 1000 << "\n" << std::flush;
            if (good && audit_) {
                const unsigned long long opportunity_sequence = ++opportunity_sequence_;
                audit_ << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
                       << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':' << ws_session_id_ << ':' << item.first << ":opportunity:" << opportunity_sequence
                       << "\",\"run_id\":\"" << run_id_ << "\",\"evaluation_sequence\":" << opportunity_sequence
                       << ",\"event_type\":\"shadow_opportunity\",\"strategy\":\"paired_lock\",\"market_id\":\"" << item.first
                       << "\",\"condition_id\":\"" << reference_ipc::escaped(item.second.condition_id)
                       << "\",\"asset\":\"" << reference_ipc::escaped(item.second.asset)
                       << "\",\"timeframe\":\"" << reference_ipc::escaped(item.second.interval)
                       << "\",\"window\":\"" << reference_ipc::escaped(item.second.window)
                       << "\",\"generation\":" << generation_ << ",\"session\":" << ws_session_id_
                       << ",\"subscription_generation\":" << generation_ << ",\"ws_session_id\":" << ws_session_id_
                       << ",\"decision\":\"ACCEPT\",\"reason\":\"opportunity\",\"target_size\":" << size_
                       << ",\"up_vwap\":" << up.second << ",\"down_vwap\":" << down.second
                       << ",\"up_cost\":" << size_ * up.second << ",\"down_cost\":" << size_ * down.second
                       << ",\"gross_cost\":" << gross_cost << ",\"up_fee\":" << up_fee << ",\"down_fee\":" << down_fee
                       << ",\"total_fees\":" << up_fee + down_fee << ",\"fee_rate\":" << rate
                       << ",\"execution_buffer\":" << buffer << ",\"buffer\":" << buffer
                       << ",\"net_cost\":" << net_cost << ",\"guaranteed_payout\":" << size_
                       << ",\"locked_profit\":" << profit << ",\"locked_roi\":" << (net_cost > 0 ? profit / net_cost : 0)
                       << ",\"up_depth_ok\":" << (up.first >= size_ ? "true" : "false")
                       << ",\"down_depth_ok\":" << (down.first >= size_ ? "true" : "false")
                       << ",\"fok\":true,\"books_ready\":true,\"books_fresh\":true,\"books_synced\":true"
                       << ",\"up_age_ms\":" << up_age_ms << ",\"down_age_ms\":" << down_age_ms
                       << ",\"book_skew_ms\":" << std::abs(up_book.source_timestamp_ms - down_book.source_timestamp_ms)
                       << ",\"leg_1_fill_probability\":" << leg_1_fill_probability
                       << ",\"leg_2_fill_probability\":" << leg_2_fill_probability
                       << ",\"time_between_legs_us\":" << leg_interval_us_
                       << ",\"orphan_leg_loss\":" << orphan_leg_loss
                       << ",\"expected_execution_value\":" << expected_execution_value
                       << ",\"execution_model\":\"configured_latency_stress\""
                       << ",\"seconds_to_close\":" << seconds_to_close
                       << ",\"duration_ms\":" << (timestamp - item.second.active_since) * 1000
                       << ",\"config_version\":\"paired-lock-shadow-v2\",\"config_hash\":\"" << paired_config_hash_
                       << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n" << std::flush;
            }
        }
        evaluate_reference_strategies();
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
        const double reference_receive_age_ms = reference_receive_at_.time_since_epoch().count()
            ? std::chrono::duration<double, std::milli>(
                std::chrono::steady_clock::now() - reference_receive_at_).count()
            : -1;
        out << "{\"updated_at\":" << now_seconds() << ",\"ws_connected\":" << (connected ? "true" : "false")
            << ",\"ws_session_id\":" << ws_session_id_ << ",\"subscription_generation\":" << generation_
            << ",\"document_version\":" << document_version_ << ",\"markets\":" << markets_.size()
            << ",\"tokens\":" << books_.size() << ",\"ready_markets\":" << ready
            << ",\"waiting_up_snapshot\":" << waiting_up << ",\"waiting_down_snapshot\":" << waiting_down
            << ",\"last_market_data_at\":" << last_activity_ << ",\"full_resyncs\":" << full_resync_count_
            << ",\"reference_connected\":" << (reference_connected_ ? "true" : "false")
            << ",\"reference_sequence\":" << reference_snapshot_.sequence
            << ",\"reference_producer_session\":\"" << reference_ipc::escaped(reference_snapshot_.producer_session) << "\""
            << ",\"reference_protocol_errors\":" << (reference_client_ ? reference_client_->protocol_errors() : 0)
            << ",\"reference_reconnects\":" << (reference_client_ ? reference_client_->reconnects() : 0)
            << ",\"reference_coalesced_frames\":" << (reference_client_ ? reference_client_->coalesced_frames() : 0)
            << ",\"strategy_audit_queue\":" << strategy_audit_.queued()
            << ",\"strategy_audit_backpressure\":" << strategy_audit_backpressure_
            << ",\"strategy_evaluations\":" << strategy_evaluation_sequence_
            << ",\"paired_config_hash\":\"" << paired_config_hash_ << "\""
            << ",\"inventory_config_hash\":\"" << inventory_strategy_config_hash_ << "\""
            << ",\"maker_config_hash\":\"" << maker_strategy_config_hash_ << "\""
            << ",\"reference_receive_age_ms\":";
        if (reference_receive_age_ms >= 0) out << reference_receive_age_ms;
        else out << "null";
        out << ",\"reference_ipc_receive_age_ms_latest\":";
        if (reference_ipc_receive_age_ms_.count()) out << reference_ipc_receive_age_ms_.latest();
        else out << "null";
        out << ",\"reference_ipc_receive_age_ms_p95\":";
        if (reference_ipc_receive_age_ms_.count()) out << reference_ipc_receive_age_ms_.percentile(.95);
        else out << "null";
        out << ",\"reference_ipc_receive_age_samples\":" << reference_ipc_receive_age_ms_.count()
            << ",\"clob_to_strategy_evaluation_us_latest\":";
        if (clob_to_strategy_evaluation_us_.count()) out << clob_to_strategy_evaluation_us_.latest();
        else out << "null";
        out << ",\"clob_to_strategy_evaluation_us_p95\":";
        if (clob_to_strategy_evaluation_us_.count()) out << clob_to_strategy_evaluation_us_.percentile(.95);
        else out << "null";
        out << ",\"clob_to_strategy_evaluation_samples\":" << clob_to_strategy_evaluation_us_.count();
        out
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

    void schedule_evaluation() {
        evaluation_timer_.expires_after(std::chrono::milliseconds(250));
        evaluation_timer_.async_wait([self = shared_from_this()](beast::error_code ec) {
            if (ec || self->stopped_) return;
            self->evaluate();
            self->schedule_evaluation();
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
            bool inventory_changed = false;
            for (auto item = complete_set_inventory_.begin();
                 item != complete_set_inventory_.end();) {
                if (!markets_.count(item->first)) {
                    inventory_origin_config_hashes_.erase(item->first);
                    item = complete_set_inventory_.erase(item);
                    inventory_changed = true;
                } else {
                    ++item;
                }
            }
            if (inventory_changed) save_complete_set_inventory();
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
        if (operation.empty()) message += "],\"type\":\"market\",\"custom_feature_enabled\":true}";
        else message += "],\"operation\":\"" + operation + "\"}";
        return message;
    }

    void fail(const char* stage, beast::error_code ec) {
        if (stopped_) return;
        stopped_ = true; timer_.cancel(); reload_timer_.cancel(); evaluation_timer_.cancel();
        if (reference_client_) reference_client_->stop();
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
    asio::steady_timer evaluation_timer_;
    std::shared_ptr<reference_ipc::LatestValueClient> reference_client_;
    reference_ipc::Snapshot reference_snapshot_;
    std::chrono::steady_clock::time_point reference_receive_at_{};
    double reference_transport_age_at_receive_ms_ = 1e9;
    std::chrono::steady_clock::time_point last_clob_mutation_at_{};
    RollingMetric<2048> reference_ipc_receive_age_ms_, clob_to_strategy_evaluation_us_;
    bool reference_connected_ = false;
    std::deque<std::string> writes_;
    std::map<std::string, Market> markets_;
    std::map<std::string, Book> books_;
    double size_, fallback_fee_, buffer_per_share_, min_profit_, leg_interval_us_, execution_half_life_us_;
    double orphan_loss_per_share_, min_expected_value_, last_activity_;
    unsigned long long book_events_ = 0, price_changes_ = 0;
    bool stopped_ = false;
    std::ofstream audit_;
    BoundedAuditWriter strategy_audit_;
    std::string markets_path_, health_path_, run_id_;
    strategy::Config strategy_config_ = strategy_config_from_environment();
    std::map<std::string, complete_set::Inventory> complete_set_inventory_;
    std::map<std::string, std::string> inventory_origin_config_hashes_;
    double inventory_min_entry_edge_ = environment_double("INVENTORY_MIN_ENTRY_EDGE", "0.05");
    double inventory_min_entry_ev_roi_ = environment_double(
        "INVENTORY_MIN_ENTRY_EV_ROI", "0.25");
    double inventory_max_initial_price_ = environment_double(
        "INVENTORY_MAX_INITIAL_PRICE", "0.20");
    double inventory_max_complement_gap_ = environment_double(
        "INVENTORY_MAX_COMPLEMENT_GAP", "0.03");
    double inventory_min_locked_roi_ = environment_double(
        "INVENTORY_MIN_LOCKED_ROI", "0.02");
    double inventory_max_unmatched_notional_ = environment_double(
        "INVENTORY_MAX_UNMATCHED_NOTIONAL", "0.50");
    double inventory_max_total_unmatched_notional_ = environment_double(
        "INVENTORY_MAX_TOTAL_UNMATCHED_NOTIONAL", "3.0");
    double maker_tick_size_ = environment_double("MAKER_TICK_SIZE", "0.01");
    double maker_quote_half_spread_ = environment_double("MAKER_QUOTE_HALF_SPREAD", "0.02");
    double maker_inventory_skew_per_unit_ = environment_double(
        "MAKER_INVENTORY_SKEW_PER_UNIT", "0.005");
    double maker_expected_rebate_per_pair_ = environment_double(
        "MAKER_EXPECTED_REBATE_PER_PAIR", "0");
    double maker_minimum_pair_edge_ = environment_double("MAKER_MINIMUM_PAIR_EDGE", "0.01");
    double maker_both_fill_probability_ = environment_double(
        "MAKER_BOTH_FILL_PROBABILITY", "0");
    double maker_orphan_loss_ = environment_double("MAKER_ORPHAN_LOSS", "0.02");
    std::string inventory_state_path_ = environment_value(
        "COMPLETE_SET_INVENTORY_STATE_PATH", "state/complete-set-inventory.json");
    std::string directional_strategy_config_hash_ = strategy_config_hash("late_window_directional_ev");
    std::string lottery_strategy_config_hash_ = strategy_config_hash("low_price_lottery_ev");
    std::string inventory_strategy_config_hash_ = strategy_config_hash("inventory_rebalancing_arb");
    std::string maker_strategy_config_hash_ = strategy_config_hash("maker_complete_set_arb");
    std::string paired_config_hash_;
    std::map<std::string, std::pair<std::string, double>> strategy_emission_state_;
    double strategy_accept_heartbeat_seconds_ = 5, strategy_reject_heartbeat_seconds_ = 60;
    double last_health_write_ = 0;
    unsigned long long document_version_, generation_, ws_session_id_, full_resync_count_ = 0;
    unsigned long long evaluation_sequence_ = 0, opportunity_sequence_ = 0;
    unsigned long long strategy_evaluation_sequence_ = 0, strategy_audit_backpressure_ = 0;
    static std::atomic<unsigned long long> next_session_id_;
};

std::atomic<unsigned long long> MarketWsSession::next_session_id_{0};

int main(int argc, char** argv) {
    if (argc >= 2 && argc <= 3 && std::string(argv[1]) == "--strategy-config-hash") {
        std::cout << strategy_config_hash(argc == 3 ? argv[2] : "") << "\n";
        return 0;
    }
    if (argc < 2) { std::cerr << "usage: market_ws_engine <markets.json> [size] [fallback_fee_rate] [audit.jsonl] [buffer_per_share] [min_profit] [leg_interval_us] [execution_half_life_us] [orphan_loss_per_share] [min_expected_value] [health.json] [strategy_audit.jsonl]\n"; return 2; }
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
        const std::string strategy_audit_path = argc > 12 ? argv[12] : "logs/strategy-audit.jsonl";
        for (;;) {
            asio::io_context io; asio::ssl::context ssl(asio::ssl::context::tls_client);
            ssl.set_default_verify_paths(); ssl.set_verify_mode(asio::ssl::verify_peer);
            std::map<std::string, Book> books;
            for (const auto& item : markets) { books[item.second.up]; books[item.second.down]; }
            std::cerr << "BOOK_BOOTSTRAP_SKIPPED reason=ws_snapshot_required tokens=" << books.size() << "\n";
            auto session = std::make_shared<MarketWsSession>(io, ssl, markets, std::move(books), size, fee, buffer_per_share,
                min_profit, leg_interval_us, execution_half_life_us, orphan_loss_per_share, min_expected_value,
                audit_path, argv[1], document_version, health_path, strategy_audit_path);
            session->run(); io.run();
            std::cerr << "WS_RECONNECT delay_s=2\n";
            std::this_thread::sleep_for(std::chrono::seconds(2));
        }
    } catch (const std::exception& error) { std::cerr << "FATAL " << error.what() << "\n"; return 1; }
}
