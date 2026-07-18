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
#include "../strategy/dynamic_position_sizing.hpp"
#include "../strategy/ev_strategy.hpp"
#include "../strategy/microstructure_reversion.hpp"
#include "../strategy/observed_arb.hpp"
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
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <set>
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
    double updated_at = 0, source_timestamp_ms = 0, snapshot_timestamp_ms = 0;
    double crossed_since = 0;
    std::string hash;
    unsigned long long generation = 0, version = 0;
};
struct Market {
    std::string up, down, last_reason, split_sell_last_reason;
    std::string condition_id, asset, interval, window;
    std::string settlement_source, title, open_price_source, open_price_capture_mode;
    std::optional<double> open_price, open_price_source_timestamp_ms;
    double fee = .07, min_order_size = 0, tick_size = 0;
    double start_ts = 0, close_ts = 0, active_since = 0, last_audit = 0;
    double split_sell_active_since = 0, split_sell_last_audit = 0;
    double arb_research_last_audit = 0, arb_research_last_evaluated = 0;
    std::array<bool, 32> arb_research_qualified{};
    unsigned long long arb_research_up_version = 0;
    unsigned long long arb_research_down_version = 0;
    bool arb_research_books_synced = false;
    bool arb_research_initialized = false;
    unsigned long long last_book_evaluation_up_version = 0;
    unsigned long long last_book_evaluation_down_version = 0;
    unsigned long long last_book_evaluation_time_bucket = 0;
    unsigned long long last_strategy_up_version = 0, last_strategy_down_version = 0;
    unsigned long long last_strategy_reference_revision = 0, last_strategy_time_bucket = 0;
    bool accepting_orders = true;
    Market() = default;
    Market(const std::string& up_token, const std::string& down_token) : up(up_token), down(down_token) {}
};

double now_seconds();
double steady_now_us() {
    return std::chrono::duration<double, std::micro>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

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
        market.min_order_size = row.get<double>("min_order_size");
        market.tick_size = row.get<double>("tick_size");
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
        if (market.min_order_size <= 0 || market.tick_size <= 0)
            throw std::runtime_error("invalid market sizing metadata");
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
    config.directional_enforce_time_window =
        environment_value("DIRECTIONAL_ENFORCE_TIME_WINDOW", "0") != "0";
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
        {"directional_enforce_time_window", environment_value("DIRECTIONAL_ENFORCE_TIME_WINDOW", "0")},
        {"directional_window_5m_min", environment_value("DIRECTIONAL_WINDOW_5M_MIN", "5")},
        {"directional_window_5m_max", environment_value("DIRECTIONAL_WINDOW_5M_MAX", "15")},
        {"directional_window_15m_min", environment_value("DIRECTIONAL_WINDOW_15M_MIN", "5")},
        {"directional_window_15m_max", environment_value("DIRECTIONAL_WINDOW_15M_MAX", "20")},
        {"directional_window_1h_min", environment_value("DIRECTIONAL_WINDOW_1H_MIN", "8")},
        {"directional_window_1h_max", environment_value("DIRECTIONAL_WINDOW_1H_MAX", "30")},
        {"directional_window_4h_min", environment_value("DIRECTIONAL_WINDOW_4H_MIN", "10")},
        {"directional_window_4h_max", environment_value("DIRECTIONAL_WINDOW_4H_MAX", "45")},
        {"directional_settlement_buffer", environment_value("DIRECTIONAL_SETTLEMENT_BUFFER", "0.002")},
        {"directional_fractional_kelly", environment_value("DIRECTIONAL_FRACTIONAL_KELLY", "0.10")},
        {"directional_max_capital_fraction", environment_value("DIRECTIONAL_MAX_CAPITAL_FRACTION", "0.02")},
        {"directional_max_quantity", environment_value("DIRECTIONAL_MAX_QUANTITY", "100")},
        {"directional_probability_haircut", environment_value("DIRECTIONAL_PROBABILITY_HAIRCUT", "0.02")},
        {"imbalance_z", environment_value("MODEL_IMBALANCE_Z", "0.25")},
        {"inventory_max_complement_gap", environment_value("INVENTORY_MAX_COMPLEMENT_GAP", "0.03")},
        {"inventory_max_initial_price", environment_value("INVENTORY_MAX_INITIAL_PRICE", "0.20")},
        {"inventory_max_total_unmatched_notional", environment_value("INVENTORY_MAX_TOTAL_UNMATCHED_NOTIONAL", "3.0")},
        {"inventory_max_unmatched_notional", environment_value("INVENTORY_MAX_UNMATCHED_NOTIONAL", "0.50")},
        {"inventory_legacy_max_guaranteed_loss", environment_value("INVENTORY_LEGACY_MAX_GUARANTEED_LOSS", "0.50")},
        {"inventory_legacy_min_loss_reduction_ratio", environment_value("INVENTORY_LEGACY_MIN_LOSS_REDUCTION_RATIO", "0.75")},
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
        {"lottery_fractional_kelly", environment_value("LOTTERY_FRACTIONAL_KELLY", "0.025")},
        {"lottery_max_capital_fraction", environment_value("LOTTERY_MAX_CAPITAL_FRACTION", "0.005")},
        {"lottery_max_quantity", environment_value("LOTTERY_MAX_QUANTITY", "100")},
        {"lottery_probability_haircut", environment_value("LOTTERY_PROBABILITY_HAIRCUT", "0.05")},
        {"maximum_book_age_ms", environment_value("CLOB_MAX_BOOK_AGE_MS", "750")},
        {"maximum_clock_skew_ms", environment_value("MAX_CLOCK_SKEW_MS", "250")},
        {"maximum_reference_age_ms", environment_value("REFERENCE_MAX_AGE_MS", "3000")},
        {"maximum_slippage", environment_value("STRATEGY_MAX_SLIPPAGE", "0.01")},
        {"maker_both_fill_probability", environment_value("MAKER_BOTH_FILL_PROBABILITY", "0")},
        {"maker_expected_rebate_per_pair", environment_value("MAKER_EXPECTED_REBATE_PER_PAIR", "0")},
        {"maker_minimum_pair_edge", environment_value("MAKER_MINIMUM_PAIR_EDGE", "0.01")},
        {"maker_orphan_loss", environment_value("MAKER_ORPHAN_LOSS", "0.02")},
        {"maker_observation_window_seconds", environment_value(
            "MAKER_OBSERVATION_WINDOW_SECONDS", "30")},
        {"maker_quote_half_spread", environment_value("MAKER_QUOTE_HALF_SPREAD", "0.02")},
        {"maker_tick_size", environment_value("MAKER_TICK_SIZE", "0.01")},
        {"minimum_liquidity", environment_value("STRATEGY_MIN_LIQUIDITY", "20")},
        {"minimum_model_sample_span_seconds", environment_value("MODEL_MIN_SAMPLE_SPAN_SECONDS", "60")},
        {"momentum_z_per_bps", environment_value("MODEL_MOMENTUM_Z_PER_BPS", "0.002")},
        {"probability_reference", "settlement_reference"},
        {"shadow_buffer_per_share", environment_value("SHADOW_BUFFER_PER_SHARE", "0.002")},
        {"shadow_min_profit", environment_value("SHADOW_MIN_PROFIT", "0.01")},
        {"shadow_sizing_capital_usd", environment_value("SHADOW_SIZING_CAPITAL_USD", "1000")},
        {"shadow_profit_exit_buffer_per_share", environment_value(
            "SHADOW_PROFIT_EXIT_BUFFER_PER_SHARE", "0.001")},
        {"shadow_size", environment_value("SHADOW_SIZE", "10")},
        {"split_sell_buffer_per_share", environment_value(
            "SPLIT_SELL_BUFFER_PER_SHARE", "0.003")},
    };
    if (!strategy_name.empty()) {
        const auto relevant = [&](const std::string& key) {
            const bool common = key == "coinbase_reference_max_age_ms" ||
                key == "minimum_liquidity" || key == "maximum_slippage" ||
                key == "maximum_reference_age_ms" || key == "maximum_book_age_ms" ||
                key == "maximum_clock_skew_ms" || key == "minimum_model_sample_span_seconds" ||
                key == "probability_reference" ||
                key == "shadow_sizing_capital_usd" ||
                key == "shadow_profit_exit_buffer_per_share";
            if (strategy_name == "late_window_directional_ev") return common ||
                key == "directional_min_net_ev" || key == "directional_latency_buffer" ||
                 key == "directional_settlement_buffer" || key == "directional_min_probability" ||
                 key == "directional_enforce_time_window" ||
                 key == "directional_fractional_kelly" ||
                 key == "directional_max_capital_fraction" ||
                 key == "directional_max_quantity" ||
                 key == "directional_probability_haircut" ||
                key.rfind("directional_window_", 0) == 0 || key == "momentum_z_per_bps" ||
                key == "imbalance_z";
            if (strategy_name == "low_price_lottery_ev") return common ||
                key == "lottery_min_price" || key == "lottery_max_price" ||
                key == "lottery_min_net_ev" || key == "lottery_model_buffer" ||
                 key == "lottery_execution_buffer" || key == "lottery_distance_weight" ||
                 key == "lottery_fractional_kelly" ||
                 key == "lottery_max_capital_fraction" ||
                 key == "lottery_max_quantity" ||
                 key == "lottery_probability_haircut" ||
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
                               double min_expected_value, double shadow_capital_usd,
                               double maximum_capital_fraction, double maximum_quantity,
                               double minimum_locked_roi) {
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
            << ",\"minimum_locked_roi\":" << minimum_locked_roi
            << ",\"minimum_seconds_to_close\":20"
            << ",\"orphan_loss_per_share\":" << orphan_loss_per_share
            << ",\"shadow_capital_usd\":" << shadow_capital_usd
            << ",\"maximum_capital_fraction\":" << maximum_capital_fraction
            << ",\"maximum_quantity\":" << maximum_quantity
            << ",\"sizing_mode\":\"real_market_dynamic_v1\""
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

struct SessionStrategyCount {
    unsigned long long evaluations = 0;
    unsigned long long accepts = 0;
    unsigned long long rejections = 0;
};

struct MakerQuoteObservation {
    double up_bid = 0, down_bid = 0, created_at = 0;
    bool up_trade_through = false, down_trade_through = false;
    unsigned long long generation = 0, session = 0;
};

struct ProbabilityShadowPosition {
    std::string strategy, market_id, outcome, entry_event_id;
    double quantity = 0, entry_cost = 0, entry_ts = 0;
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
    const std::string direction = row.get<std::string>("side", "");
    if (direction != "BUY" && direction != "SELL") return false;
    auto& side = direction == "BUY" ? book.bids : book.asks;
    const double price = number(row, "price"), size = number(row, "size");
    if (!std::isfinite(price) || price <= 0 || !std::isfinite(size) || size < 0)
        return false;
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

std::pair<double, double> sell_vwap(const Book& book, double size) {
    double left = size, filled = 0, notional = 0;
    for (auto level = book.bids.rbegin(); level != book.bids.rend(); ++level) {
        const double take = std::min(left, level->second);
        filled += take;
        notional += take * level->first;
        left -= take;
        if (left <= 1e-9) break;
    }
    return {filled, filled ? notional / filled : 0};
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
            execution_half_life_us_, orphan_loss_per_share_, min_expected_value_,
            shadow_sizing_capital_usd_, paired_max_capital_fraction_,
            paired_max_quantity_, paired_min_locked_roi_);
        inventory_strategy_config_hash_ = sha256_hex(
            inventory_strategy_config_hash_ + "|" + std::to_string(size_) + "|" +
            std::to_string(buffer_per_share_) + "|" + std::to_string(min_profit_));
        maker_strategy_config_hash_ = sha256_hex(
            maker_strategy_config_hash_ + "|" + std::to_string(size_) + "|" +
            std::to_string(buffer_per_share_));
        split_sell_strategy_config_hash_ = sha256_hex(
            paired_config_hash_ + "|split_sell_v2|" +
            std::to_string(split_sell_buffer_per_share_));
        reversion_strategy_config_hash_ = sha256_hex(
            "microstructure-reversion-shadow-v1|" + std::to_string(size_) + "|" +
            std::to_string(reversion_lookback_ms_) + "|" +
            std::to_string(reversion_minimum_discount_per_share_) + "|" +
            std::to_string(reversion_maximum_holding_ms_) + "|" +
            std::to_string(reversion_minimum_profit_));
        load_complete_set_inventory();
        strategy_accept_heartbeat_seconds_ = environment_double("STRATEGY_ACCEPT_AUDIT_HEARTBEAT_SECONDS", "5");
        strategy_reject_heartbeat_seconds_ = environment_double("STRATEGY_REJECT_AUDIT_HEARTBEAT_SECONDS", "60");
        for (const std::string strategy : {
                 "late_window_directional_ev", "low_price_lottery_ev",
                 "paired_lock", "microstructure_reversion", "split_sell_lock",
                 "maker_complete_set_arb",
             }) {
            session_strategy_counts_[strategy];
        }
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

    double probability_input_quality(
            const strategy::ProbabilityInput& model_input,
            const ReferenceView& reference,
            const std::optional<double>& estimated_probability) const {
        if (!estimated_probability) return 0;
        return std::clamp(
            std::min(model_input.model_sample_count / 120.0, 1.0) *
                (1 - std::min(reference.divergence_bps.value_or(0) / 100, 1.0)),
            0.0, 1.0);
    }

    sizing::ProbabilityConfig probability_sizing_config(
            const std::string& strategy_name, const Market& market,
            double fee_rate) const {
        const bool lottery = strategy_name == "low_price_lottery_ev";
        sizing::ProbabilityConfig config;
        config.shadow_capital_usd = shadow_sizing_capital_usd_;
        config.fractional_kelly = lottery
            ? lottery_fractional_kelly_ : directional_fractional_kelly_;
        config.maximum_capital_fraction = lottery
            ? lottery_max_capital_fraction_ : directional_max_capital_fraction_;
        config.probability_haircut = lottery
            ? lottery_probability_haircut_ : directional_probability_haircut_;
        config.maximum_quantity = lottery
            ? lottery_max_quantity_ : directional_max_quantity_;
        config.minimum_order_size = market.min_order_size;
        config.maximum_slippage_per_share = strategy_config_.maximum_slippage;
        config.minimum_net_ev_per_share = lottery
            ? strategy_config_.lottery_min_net_ev : strategy_config_.directional_min_net_ev;
        config.fee_rate = fee_rate;
        config.execution_buffer_per_share = lottery
            ? strategy_config_.lottery_model_buffer + strategy_config_.lottery_execution_buffer
            : strategy_config_.directional_latency_buffer + strategy_config_.directional_settlement_buffer;
        return config;
    }

    static void apply_sizing_rejection(
            strategy::Decision& decision, const sizing::Result& sizing_result) {
        if (sizing_result.accepted) return;
        const std::string reason = sizing_result.reason.empty()
            ? "dynamic_size_unavailable" : sizing_result.reason;
        strategy::append_reason(decision.blocking_reasons, reason);
        decision.decision = "REJECT";
        if (decision.reason.empty() || decision.reason == "positive_net_ev")
            decision.reason = reason;
    }

    static std::string probability_position_key(
            const std::string& strategy_name, const std::string& market_id,
            const std::string& outcome) {
        return strategy_name + "|" + market_id + "|" + outcome;
    }

    void remember_probability_shadow_position(
            const std::string& market_id, const std::string& outcome,
            const strategy::Decision& decision,
            const sizing::Result& sizing_result, const std::string& event_id,
            double timestamp) {
        if (decision.decision != "ACCEPT" ||
            (decision.strategy != "late_window_directional_ev" &&
             decision.strategy != "low_price_lottery_ev")) return;
        const std::string key = probability_position_key(
            decision.strategy, market_id, outcome);
        if (active_probability_shadow_positions_.count(key)) return;
        if (!sizing_result.accepted || sizing_result.dynamic_target_size <= 0) return;
        active_probability_shadow_positions_[key] = {
            decision.strategy, market_id, outcome, event_id,
            sizing_result.dynamic_target_size,
            sizing_result.dynamic_all_in_cost, timestamp,
        };
    }

    bool emit_probability_profit_exit(
            const std::string& market_id, const Market& market,
            const std::string& outcome, const Book& outcome_book,
            const strategy::EvaluationInput& input,
            const std::string& strategy_name, double fee_rate,
            double timestamp) {
        const std::string key = probability_position_key(
            strategy_name, market_id, outcome);
        const auto found = active_probability_shadow_positions_.find(key);
        if (found == active_probability_shadow_positions_.end()) return false;
        const ProbabilityShadowPosition& position = found->second;
        if (timestamp <= position.entry_ts ||
            input.book_age_ms > input.maximum_book_age_ms) return false;
        const auto exit_fill = sell_vwap(outcome_book, position.quantity);
        if (exit_fill.first + 1e-12 < position.quantity || exit_fill.second <= 0)
            return false;
        const double exit_fee = std::round(
            exit_fill.first * fee_rate * exit_fill.second *
            (1 - exit_fill.second) * 100000) / 100000;
        const double exit_buffer = position.quantity * profit_exit_buffer_per_share_;
        const double net_proceeds =
            position.quantity * exit_fill.second - exit_fee - exit_buffer;
        const double profit = net_proceeds - position.entry_cost;
        if (profit + 1e-12 < profit_exit_min_pnl_) return false;

        const unsigned long long sequence = ++strategy_evaluation_sequence_;
        const std::string event_id = run_id_ + ":" +
            std::to_string(generation_) + ":" +
            std::to_string(ws_session_id_) + ":" + market_id + ":" +
            strategy_name + ":profit-exit:" + std::to_string(sequence);
        std::ostringstream out;
        out << std::setprecision(15)
            << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
            << ",\"event_id\":\"" << reference_ipc::escaped(event_id)
            << "\",\"entry_event_id\":\""
            << reference_ipc::escaped(position.entry_event_id)
            << "\",\"event_type\":\"shadow_probability_profit_exit_book_executable\""
            << ",\"strategy\":\"" << reference_ipc::escaped(strategy_name)
            << "\",\"market_id\":\"" << reference_ipc::escaped(market_id)
            << "\",\"condition_id\":\""
            << reference_ipc::escaped(market.condition_id)
            << "\",\"asset\":\"" << reference_ipc::escaped(market.asset)
            << "\",\"timeframe\":\"" << reference_ipc::escaped(market.interval)
            << "\",\"window\":\"" << reference_ipc::escaped(market.window)
            << "\",\"outcome\":\"" << reference_ipc::escaped(outcome)
            << "\",\"generation\":" << generation_
            << ",\"session\":" << ws_session_id_
            << ",\"evaluation_sequence\":" << sequence
            << ",\"target_size\":" << position.quantity
            << ",\"entry_cost\":" << position.entry_cost
            << ",\"exit_fill_quantity\":" << exit_fill.first
            << ",\"exit_vwap\":" << exit_fill.second
            << ",\"exit_total_fee\":" << exit_fee
            << ",\"exit_execution_buffer\":" << exit_buffer
            << ",\"exit_net_proceeds\":" << net_proceeds
            << ",\"expected_profit\":" << profit
            << ",\"minimum_profit\":" << profit_exit_min_pnl_
            << ",\"exit_depth_ok\":true,\"exit_book_fresh\":true"
            << ",\"observation_semantics\":\"BOOK_EXECUTABLE_NOT_FILL\""
            << ",\"exit_observation_semantics\":\"BOOK_EXECUTABLE_NOT_FILL\""
            << ",\"simulated_fill\":false,\"decision\":\"OBSERVED\""
            << ",\"reason\":\"profit_target_book_executable\""
            << ",\"config_version\":\"shadow-buy-rules-v9\""
            << ",\"config_hash\":\"" << strategy_hash_for(strategy_name) << "\""
            << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        if (!strategy_audit_.enqueue(out.str())) {
            ++strategy_audit_backpressure_;
            return false;
        }
        active_probability_shadow_positions_.erase(found);
        return true;
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
            const double rate = market.fee > 0 ? market.fee : fallback_fee_;
            std::map<std::string, strategy::EvaluationInput> directional_inputs;
            for (const std::string outcome : {"Up", "Down"}) {
                const bool is_up = outcome == "Up";
                const Book& book = is_up ? up_book : down_book;
                const double ask = best_ask(book);
                const std::optional<double> directional_raw_probability = !directional_probability.estimated_probability
                    ? std::nullopt
                    : is_up ? directional_probability.estimated_probability
                            : std::optional<double>(1 - *directional_probability.estimated_probability);
                const std::optional<double> lottery_raw_probability = !lottery_probability.estimated_probability
                    ? std::nullopt
                    : is_up ? lottery_probability.estimated_probability
                            : std::optional<double>(1 - *lottery_probability.estimated_probability);
                for (const std::string strategy_name : {"late_window_directional_ev", "low_price_lottery_ev"}) {
                    const bool is_lottery = strategy_name == "low_price_lottery_ev";
                    const auto raw_probability = is_lottery
                        ? lottery_raw_probability : directional_raw_probability;
                    strategy::EvaluationInput input;
                    input.strategy = strategy_name;
                    input.estimated_probability = is_lottery
                        ? strategy::lottery_market_blend_probability(raw_probability, ask, strategy_config_)
                        : raw_probability;
                    const double input_quality = probability_input_quality(
                        probability_input, reference, input.estimated_probability);
                    const auto sizing_result = sizing::size_probability_position(
                        book.asks, input.estimated_probability.value_or(
                            std::numeric_limits<double>::quiet_NaN()),
                        input_quality,
                        probability_sizing_config(strategy_name, market, rate));
                    const double evaluation_quantity = sizing_result.accepted
                        ? sizing_result.dynamic_target_size : market.min_order_size;
                    const auto fill = buy_vwap(book, evaluation_quantity);
                    const double fee = sizing_result.accepted
                        ? sizing_result.dynamic_fee
                        : std::round(fill.first * rate * fill.second *
                            (1 - fill.second) * 100000) / 100000;
                    input.timeframe = market.interval;
                    input.expected_fill_price = sizing_result.accepted
                        ? sizing_result.dynamic_vwap : fill.second;
                    input.seconds_to_close = seconds_to_close;
                    input.price_to_beat = opening_price;
                    input.fee_per_share = fee / std::max(evaluation_quantity, 1e-9);
                    // VWAP already includes depth slippage; the solver enforces its limit.
                    input.slippage_per_share = 0;
                    input.liquidity = available_ask_depth(book);
                    input.book_age_ms = book_age_ms;
                    input.reference_age_ms = reference.reference_age_ms;
                    input.clock_skew_ms = reference.clock_skew_ms;
                    input.market_active = market.close_ts > timestamp;
                    input.market_tradable = market.accepting_orders;
                    input.target_depth_ok = sizing_result.accepted &&
                        fill.first + 1e-9 >= evaluation_quantity;
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
                    strategy::Decision decision = strategy_name == "late_window_directional_ev"
                        ? strategy::evaluate_directional(input, strategy_config_)
                        : strategy::evaluate_lottery(input, strategy_config_);
                    apply_sizing_rejection(decision, sizing_result);
                    if (!is_lottery) {
                        directional_inputs[outcome] = input;
                    }
                    if (!strategy_audit_.available()) {
                        decision.decision = "REJECT";
                        decision.reason = "audit_backpressure";
                        decision.blocking_reasons.insert(decision.blocking_reasons.begin(), "audit_backpressure");
                    }
                    if (outcome == "Up" && input.estimated_probability && opening_price &&
                        reference.settlement_reference && reference.quorum &&
                        input.settlement_source_verified) {
                        const auto horizon = probability_calibration_horizons_.find(market.interval);
                        if (horizon != probability_calibration_horizons_.end() &&
                            seconds_to_close > 0 && seconds_to_close <= horizon->second) {
                            const std::string observation_key = item.first + "|" + strategy_name + "|" +
                                strategy_hash_for(strategy_name) + "|" + std::to_string(
                                    static_cast<int>(horizon->second));
                            if (!probability_observations_emitted_.count(observation_key) &&
                                emit_strategy_audit(item.first, market, outcome, input, probability_input,
                                    is_lottery ? lottery_probability : directional_probability,
                                    raw_probability, reference, decision, timestamp, ask,
                                    book, rate, sizing_result,
                                    "shadow_prediction_observation", horizon->second)) {
                                probability_observations_emitted_.insert(observation_key);
                            }
                        }
                    }
                    const bool profit_exit_emitted = emit_probability_profit_exit(
                        item.first, market, outcome, book, input, strategy_name,
                        rate, timestamp);
                    if (profit_exit_emitted) continue;
                    const std::string key = item.first + "|" + strategy_name + "|" + outcome;
                    if (!should_emit_strategy(key, decision, timestamp)) continue;
                    emit_strategy_audit(item.first, market, outcome, input, probability_input,
                                        is_lottery ? lottery_probability : directional_probability,
                                        raw_probability, reference, decision, timestamp, ask,
                                        book, rate, sizing_result);
                }
            }
            emit_complete_set_evaluations(
                item.first, market, up_book, down_book, directional_inputs,
                directional_probability.estimated_probability, timestamp);
        }
        const auto evaluation_finished = std::chrono::steady_clock::now();
        if (evaluated_any && last_clob_mutation_at_.time_since_epoch().count()) {
            clob_to_strategy_evaluation_us_.add(std::chrono::duration<double, std::micro>(
                evaluation_finished - last_clob_mutation_at_).count());
        }
        last_clob_mutation_at_ = {};
    }


    void emit_complete_set_evaluations(
            const std::string& market_id, const Market& market,
            const Book& up_book, const Book& down_book,
            const std::map<std::string, strategy::EvaluationInput>& inputs,
            const std::optional<double>& up_probability,
            double timestamp) {
        const auto up_found = inputs.find("Up");
        const auto down_found = inputs.find("Down");
        if (up_found == inputs.end() || down_found == inputs.end()) return;
        const auto& up = up_found->second;
        const auto& down = down_found->second;
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
        } else {
            maker = complete_set::evaluate_maker({
                *up_probability, best_bid(up_book), best_ask(up_book),
                best_bid(down_book), best_ask(down_book), maker_tick_size_,
                maker_quote_half_spread_, 0.0,
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
                << ",\"quote_geometry_qualified\":"
                << (maker.quote_geometry_qualified ? "true" : "false")
                << ",\"decision\":\"" << maker.decision << "\",\"reason\":\""
                << maker.reason << "\",\"config_version\":\"maker-complete-set-v1\""
                << ",\"config_hash\":\"" << maker_strategy_config_hash_ << "\""
                << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
            if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
            else {
                record_session_strategy("maker_complete_set_arb", maker.decision);
                if (maker.quote_geometry_qualified) {
                    remember_maker_quote(market_id, maker, timestamp);
                }
            }
        }
    }

    void remember_maker_quote(
            const std::string& market_id, const complete_set::MakerDecision& maker,
            double timestamp) {
        auto& observation = maker_quote_observations_[market_id];
        const bool changed = observation.generation != generation_ ||
            observation.session != ws_session_id_ ||
            std::abs(observation.up_bid - maker.up_bid) > 1e-12 ||
            std::abs(observation.down_bid - maker.down_bid) > 1e-12;
        if (!changed) return;
        observation = {
            maker.up_bid, maker.down_bid, timestamp, false, false,
            generation_, ws_session_id_,
        };
        ++maker_quote_geometry_candidates_;
    }

    void observe_maker_trade(const ptree& message, const std::string& token) {
        const double trade_price = message.get<double>("price", 0);
        const double trade_size = message.get<double>("size", 0);
        if (trade_price <= 0 || trade_size <= 0) return;
        ++maker_trade_events_;
        const double timestamp = now_seconds();
        for (const auto& item : markets_) {
            const bool up_token = item.second.up == token;
            const bool down_token = item.second.down == token;
            if (!up_token && !down_token) continue;
            const auto found = maker_quote_observations_.find(item.first);
            if (found == maker_quote_observations_.end()) return;
            auto& observation = found->second;
            if (observation.generation != generation_ ||
                observation.session != ws_session_id_ ||
                timestamp - observation.created_at > maker_observation_window_seconds_) {
                maker_quote_observations_.erase(found);
                return;
            }
            bool touched = false;
            if (up_token && !observation.up_trade_through &&
                trade_price <= observation.up_bid + 1e-12) {
                observation.up_trade_through = true;
                touched = true;
            }
            if (down_token && !observation.down_trade_through &&
                trade_price <= observation.down_bid + 1e-12) {
                observation.down_trade_through = true;
                touched = true;
            }
            if (!touched) return;
            ++maker_single_leg_trade_throughs_;
            const bool both = observation.up_trade_through &&
                observation.down_trade_through;
            if (both) ++maker_both_leg_trade_throughs_;
            const unsigned long long sequence = ++strategy_evaluation_sequence_;
            std::ostringstream out;
            out << std::setprecision(15)
                << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
                << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':'
                << ws_session_id_ << ':' << item.first << ":maker_trade_through:"
                << sequence << "\",\"event_type\":\""
                << (both ? "shadow_maker_both_legs_trade_through"
                         : "shadow_maker_single_leg_trade_through")
                << "\",\"strategy\":\"maker_complete_set_arb\",\"market_id\":\""
                << reference_ipc::escaped(item.first) << "\",\"condition_id\":\""
                << reference_ipc::escaped(item.second.condition_id)
                << "\",\"asset\":\"" << reference_ipc::escaped(item.second.asset)
                << "\",\"timeframe\":\""
                << reference_ipc::escaped(item.second.interval)
                << "\",\"window\":\"" << reference_ipc::escaped(item.second.window)
                << "\",\"generation\":" << generation_ << ",\"session\":"
                << ws_session_id_ << ",\"evaluation_sequence\":" << sequence
                << ",\"trade_token\":\"" << reference_ipc::escaped(token)
                << "\",\"trade_price\":" << trade_price << ",\"trade_size\":"
                << trade_size << ",\"trade_side\":\""
                << reference_ipc::escaped(message.get<std::string>("side", ""))
                << "\",\"up_bid_quote\":" << observation.up_bid
                << ",\"down_bid_quote\":" << observation.down_bid
                << ",\"up_trade_through\":"
                << (observation.up_trade_through ? "true" : "false")
                << ",\"down_trade_through\":"
                << (observation.down_trade_through ? "true" : "false")
                << ",\"quote_age_ms\":"
                << (timestamp - observation.created_at) * 1000
                << ",\"observation_semantics\":\"price_reached_quote_not_queue_fill\""
                << ",\"simulated_fill\":false,\"decision\":\"OBSERVED\",\"reason\":\""
                << (both ? "both_legs_trade_through_observed"
                         : "single_leg_trade_through_observed")
                << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
            if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
            if (both) maker_quote_observations_.erase(found);
            return;
        }
    }

    bool emit_strategy_audit(const std::string& market_id, const Market& market,
                             const std::string& outcome, const strategy::EvaluationInput& input,
                             const strategy::ProbabilityInput& model_input,
                             const strategy::ProbabilityOutput& model,
                             const std::optional<double>& raw_estimated_probability,
                             const ReferenceView& reference, const strategy::Decision& decision,
                             double timestamp, double market_price,
                             const Book& outcome_book, double fee_rate,
                             const sizing::Result& sizing_result,
                             const std::string& event_type = "shadow_eval",
                             double calibration_horizon_seconds = 0) {
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
        const double target_size = sizing_result.accepted
            ? sizing_result.dynamic_target_size : 0;
        const auto exit_fill = sell_vwap(outcome_book, target_size);
        const double exit_fee = std::round(
            exit_fill.first * fee_rate * exit_fill.second *
            (1 - exit_fill.second) * 100000
        ) / 100000;
        const double exit_buffer = target_size * profit_exit_buffer_per_share_;
        const bool exit_book_fresh = input.book_age_ms <= input.maximum_book_age_ms;
        out << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
            << ",\"event_id\":\"" << reference_ipc::escaped(event_id)
            << "\",\"event_type\":\"" << event_type << "\",\"strategy\":\"" << decision.strategy
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
        out << ",\"fees\":" << input.fee_per_share << ",\"slippage\":";
        if (sizing_result.accepted)
            out << std::max(0.0, sizing_result.dynamic_vwap - market_price);
        else out << "null";
        out
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
        const double confidence = sizing_result.input_quality_score;
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
        out << "],\"sizing_mode\":\"real_market_dynamic_v1\""
            << ",\"requested_max_size\":" << sizing_result.requested_max_size
            << ",\"dynamic_target_size\":" << sizing_result.dynamic_target_size
            << ",\"market_minimum_size\":" << sizing_result.market_minimum_size
            << ",\"executable_depth_size\":" << sizing_result.executable_depth_size
            << ",\"slippage_limited_size\":" << sizing_result.slippage_limited_size
            << ",\"capital_limited_size\":" << sizing_result.capital_limited_size
            << ",\"shadow_capital_usd\":" << sizing_result.shadow_capital_usd
            << ",\"capital_budget_usd\":" << sizing_result.capital_budget_usd
            << ",\"input_quality_score\":" << sizing_result.input_quality_score
            << ",\"conservative_probability\":";
        if (input.estimated_probability) out << sizing_result.conservative_probability;
        else out << "null";
        out << ",\"probability_haircut\":" << sizing_result.probability_haircut
            << ",\"full_kelly_fraction\":" << sizing_result.full_kelly_fraction
            << ",\"applied_kelly_fraction\":" << sizing_result.applied_kelly_fraction
            << ",\"dynamic_vwap\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_vwap; else out << "null";
        out << ",\"dynamic_fee\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_fee; else out << "null";
        out << ",\"dynamic_buffer\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_buffer; else out << "null";
        out << ",\"dynamic_all_in_cost\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_all_in_cost; else out << "null";
        out << ",\"dynamic_all_in_price\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_all_in_price; else out << "null";
        out << ",\"dynamic_expected_profit\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_expected_profit; else out << "null";
        out << ",\"dynamic_maximum_loss\":";
        if (sizing_result.accepted) out << sizing_result.dynamic_maximum_loss; else out << "null";
        out << ",\"size_binding_constraint\":";
        if (sizing_result.size_binding_constraint.empty()) out << "null";
        else out << '"' << reference_ipc::escaped(sizing_result.size_binding_constraint) << '"';
        out << ",\"target_size\":" << target_size
            << ",\"exit_fill_quantity\":" << exit_fill.first
            << ",\"exit_vwap\":" << exit_fill.second
            << ",\"exit_total_fee\":" << exit_fee
            << ",\"exit_execution_buffer\":" << exit_buffer
            << ",\"exit_depth_ok\":"
            << (target_size > 0 && exit_fill.first >= target_size ? "true" : "false")
            << ",\"exit_book_fresh\":"
            << (exit_book_fresh ? "true" : "false")
            << ",\"exit_observation_semantics\":\"BOOK_EXECUTABLE_NOT_FILL\"";
        if (event_type == "shadow_prediction_observation") {
            out << ",\"opens_position\":false"
                << ",\"observation_semantics\":\"PROBABILITY_CALIBRATION_NOT_ORDER\""
                << ",\"calibration_horizon_seconds\":" << calibration_horizon_seconds;
        }
        out << ",\"config_version\":\"shadow-buy-rules-v9\""
            << ",\"config_hash\":\"" << strategy_hash_for(decision.strategy) << "\""
            << ",\"reference_sequence\":" << reference_snapshot_.sequence
            << ",\"reference_producer_session\":\"" << reference_ipc::escaped(reference_snapshot_.producer_session) << "\""
            << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        const bool queued = strategy_audit_.enqueue(out.str());
        if (!queued) ++strategy_audit_backpressure_;
        else if (event_type == "shadow_eval") {
            record_session_strategy(decision.strategy, decision.decision);
            remember_probability_shadow_position(
                market_id, outcome, decision, sizing_result, event_id, timestamp);
        }
        return queued;
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
        queue_write(subscription(assets, ""));
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
            books_[asset].snapshot_timestamp_ms = books_[asset].source_timestamp_ms;
            books_[asset].crossed_since = 0;
            books_[asset].hash = message.get<std::string>("hash", "");
            ++books_[asset].version;
            last_clob_mutation_at_ = std::chrono::steady_clock::now();
            ++book_events_;
        } else if (type == "price_change") {
            const double source_timestamp = message.get<double>("timestamp", 0);
            auto changes = message.get_child_optional("price_changes");
            if (!changes) return;
            bool changed = false;
            std::set<std::string> touched_tokens;
            std::map<std::string, std::string> resync_reasons;
            for (const auto& item : *changes) {
                const auto& row = item.second; const std::string token = row.get<std::string>("asset_id", asset);
                if (!books_.count(token) || books_[token].generation != generation_) continue;
                if (!books_[token].ws_snapshot) continue;
                if (resync_reasons.count(token)) continue;
                if (source_timestamp && books_[token].snapshot_timestamp_ms &&
                    source_timestamp <= books_[token].snapshot_timestamp_ms) {
                    ++stale_price_changes_ignored_;
                    continue;
                }
                if (source_timestamp && books_[token].source_timestamp_ms &&
                    source_timestamp < books_[token].source_timestamp_ms) {
                    ++stale_price_changes_ignored_;
                    continue;
                }
                if (!update_level(books_[token], row)) {
                    resync_reasons[token] = "invalid_level_update";
                    continue;
                }
                books_[token].updated_at = now_seconds();
                books_[token].source_timestamp_ms = source_timestamp;
                books_[token].hash = row.get<std::string>("hash", books_[token].hash);
                ++books_[token].version;
                changed = true;
                touched_tokens.insert(token);
            }
            const double batch_applied_at = now_seconds();
            for (const auto& token : touched_tokens) {
                if (resync_reasons.count(token)) continue;
                if (crossed(books_[token])) {
                    if (books_[token].crossed_since == 0)
                        books_[token].crossed_since = batch_applied_at;
                } else {
                    books_[token].crossed_since = 0;
                }
            }
            for (const auto& item : resync_reasons)
                resync_token(item.first, item.second);
            if (changed) last_clob_mutation_at_ = std::chrono::steady_clock::now();
            ++price_changes_;
        } else if (type == "last_trade_price" && books_.count(asset)) {
            observe_maker_trade(message, asset);
        }
        if (type == "book" || (type == "price_change" && price_changes_ % 100 == 0))
            std::cerr << "WS_DATA type=" << type << " books=" << book_events_ << " changes=" << price_changes_ << "\n";
    }

    struct PendingArbObservation {
        observed_arb::Attempt attempt;
        std::string episode_key, market_id, asset, timeframe, window;
        double close_ts = 0, delay_us = 0, started_wall = 0;
        double initial_up_vwap = 0, initial_down_vwap = 0;
        double initial_up_fee = 0, initial_down_fee = 0;
        double initial_net_cost = 0, initial_locked_profit = 0;
        bool first_is_up = true;
    };

    static const char* leg_order_name(observed_arb::LegOrder order) {
        return order == observed_arb::LegOrder::UP_THEN_DOWN
            ? "UP_THEN_DOWN" : "DOWN_THEN_UP";
    }

    observed_arb::BookLeg observed_buy_leg(
            const Book& book, double target_size, double rate,
            bool books_synced) const {
        const auto fill = buy_vwap(book, target_size);
        observed_arb::BookLeg leg;
        leg.requested_quantity = target_size;
        leg.executable_quantity = fill.first;
        leg.vwap = fill.second;
        leg.gross_value = fill.first * fill.second;
        leg.rounded_fee = std::round(
            fill.first * rate * fill.second * (1 - fill.second) * 100000) / 100000;
        leg.age_ms = std::max(0.0, (now_seconds() - book.updated_at) * 1000);
        leg.snapshot = book.ws_snapshot;
        leg.fresh = books_synced;
        leg.synced = books_synced;
        leg.crossed = crossed(book);
        leg.generation = book.generation;
        leg.session = ws_session_id_;
        return leg;
    }

    observed_arb::BookLeg observed_sell_leg(
            const Book& book, double target_size, double rate,
            bool books_synced) const {
        const auto fill = sell_vwap(book, target_size);
        observed_arb::BookLeg leg;
        leg.requested_quantity = target_size;
        leg.executable_quantity = fill.first;
        leg.vwap = fill.second;
        leg.gross_value = fill.first * fill.second;
        leg.rounded_fee = std::round(
            fill.first * rate * fill.second * (1 - fill.second) * 100000) / 100000;
        leg.age_ms = std::max(0.0, (now_seconds() - book.updated_at) * 1000);
        leg.snapshot = book.ws_snapshot;
        leg.fresh = books_synced;
        leg.synced = books_synced;
        leg.crossed = crossed(book);
        leg.generation = book.generation;
        leg.session = ws_session_id_;
        return leg;
    }

    std::string arb_episode_key(
            const std::string& market_id, double target_size, double delay_us,
            observed_arb::LegOrder order) const {
        std::ostringstream key;
        key << paired_config_hash_ << '|' << market_id << '|' << target_size
            << '|' << delay_us << '|' << leg_order_name(order);
        return key.str();
    }

    void queue_arb_audit(std::string record) {
        if (arb_audit_queue_.size() >= max_arb_audit_queue_) {
            ++arb_audit_backpressure_;
            return;
        }
        arb_audit_queue_.push_back(std::move(record));
    }

    void flush_arb_audit_queue() {
        if (!audit_ || arb_audit_queue_.empty()) return;
        while (!arb_audit_queue_.empty()) {
            audit_ << arb_audit_queue_.front();
            arb_audit_queue_.pop_front();
        }
        audit_.flush();
    }

    void emit_arb_episode(
            const char* event_label, const char* event_name,
            const std::string& event_id, const std::string& market_id,
            const Market& market, double target_size, double delay_us,
            observed_arb::LegOrder order, const std::string& reason) {
        const double timestamp = now_seconds();
        std::ostringstream record;
        record << std::setprecision(15)
               << '{' << event_label << ",\"ts\":" << timestamp
               << ",\"timestamp\":" << timestamp << ",\"event_id\":\""
               << event_id << ':' << event_name << "\",\"strategy\":\"paired_lock\""
               << ",\"market_id\":\"" << market_id << "\",\"condition_id\":\""
               << reference_ipc::escaped(market.condition_id)
               << "\",\"asset\":\"" << reference_ipc::escaped(market.asset)
               << "\",\"timeframe\":\"" << reference_ipc::escaped(market.interval)
               << "\",\"window\":\"" << reference_ipc::escaped(market.window)
               << "\",\"close_ts\":" << market.close_ts
               << ",\"generation\":" << generation_ << ",\"session\":"
               << ws_session_id_ << ",\"leg_order\":\"" << leg_order_name(order)
               << "\",\"delay_ms\":" << delay_us / 1000
               << ",\"target_size\":" << target_size
               << ",\"decision\":\"OBSERVED\",\"reason\":\"" << reason
               << "\",\"observation_semantics\":\"BOOK_EXECUTABLE_NOT_FILL\""
               << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        queue_arb_audit(record.str());
    }

    void emit_arb_observation(
            const char* event_label, const char* event_name,
            const PendingArbObservation& pending,
            const observed_arb::Outcome* outcome,
            const std::string& reason, int leg_index = 0) {
        const double timestamp = now_seconds();
        const double delayed_net_cost = outcome ? outcome->net_cost : 0;
        const double book_executable_quantity =
            outcome && outcome->both_legs_book_executable
                ? pending.attempt.target_size : 0;
        std::ostringstream record;
        record << std::setprecision(15)
               << '{' << event_label << ",\"ts\":" << timestamp
               << ",\"timestamp\":" << timestamp << ",\"event_id\":\""
               << pending.attempt.identity.attempt_id << ':' << event_name;
        if (leg_index) record << ':' << leg_index;
        record << "\",\"attempt_id\":\"" << pending.attempt.identity.attempt_id
               << "\",\"strategy\":\"paired_lock\",\"market_id\":\""
               << pending.market_id << "\",\"condition_id\":\""
               << reference_ipc::escaped(pending.attempt.identity.condition_id)
               << "\",\"asset\":\"" << reference_ipc::escaped(pending.asset)
               << "\",\"timeframe\":\"" << reference_ipc::escaped(pending.timeframe)
               << "\",\"window\":\"" << reference_ipc::escaped(pending.window)
               << "\",\"close_ts\":" << pending.close_ts
               << ",\"generation\":" << pending.attempt.identity.generation
               << ",\"session\":" << pending.attempt.identity.session
               << ",\"leg_order\":\"" << leg_order_name(pending.attempt.order)
               << "\",\"delay_ms\":" << pending.delay_us / 1000
               << ",\"observed_delay_ms\":"
               << std::max(0.0, steady_now_us() - pending.attempt.started_us) / 1000
               << ",\"target_size\":" << pending.attempt.target_size
               << ",\"initial_up_vwap\":" << pending.initial_up_vwap
               << ",\"initial_down_vwap\":" << pending.initial_down_vwap
               << ",\"initial_up_fee\":" << pending.initial_up_fee
               << ",\"initial_down_fee\":" << pending.initial_down_fee
               << ",\"initial_net_cost\":" << pending.initial_net_cost
               << ",\"initial_locked_profit\":" << pending.initial_locked_profit
               << ",\"delayed_net_cost\":" << delayed_net_cost
               << ",\"delayed_locked_profit\":"
               << (outcome ? outcome->locked_profit : 0)
               << ",\"book_executable_quantity\":" << book_executable_quantity
               << ",\"first_leg_book_executable\":"
               << (pending.attempt.valid ? "true" : "false")
               << ",\"both_legs_book_executable\":"
               << (outcome && outcome->both_legs_book_executable ? "true" : "false")
               << ",\"orphan_pnl\":" << (outcome ? outcome->orphan_pnl : 0)
               << ",\"leg_index\":" << leg_index
               << ",\"decision\":\"OBSERVED\",\"reason\":\"" << reason
               << "\",\"observation_semantics\":\"BOOK_EXECUTABLE_NOT_FILL\""
               << ",\"config_version\":\"observed-arb-v1\",\"config_hash\":\""
               << paired_config_hash_
               << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        queue_arb_audit(record.str());
    }

    void update_observed_arbitrage(
            const std::string& market_id, const Market& market,
            const Book& up_book, const Book& down_book, double rate,
            bool books_synced, double timestamp) {
        const double seconds_to_close = market.close_ts - timestamp;
        for (const double target_size : counterfactual_sizes_) {
            const auto up = observed_buy_leg(up_book, target_size, rate, books_synced);
            const auto down = observed_buy_leg(down_book, target_size, rate, books_synced);
            const double buffer = target_size * buffer_per_share_;
            const double initial_net_cost = up.gross_value + up.rounded_fee +
                down.gross_value + down.rounded_fee + buffer;
            const double initial_profit = target_size - initial_net_cost;
            const bool qualified = books_synced && up.executable_quantity >= target_size &&
                down.executable_quantity >= target_size && initial_profit > 0 &&
                seconds_to_close >= 20 && seconds_to_close <= 7200;
            for (const auto order : {
                    observed_arb::LegOrder::UP_THEN_DOWN,
                    observed_arb::LegOrder::DOWN_THEN_UP}) {
                for (const double delay_us : counterfactual_delays_us_) {
                    const std::string key = arb_episode_key(
                        market_id, target_size, delay_us, order);
                    const auto active = active_arb_episodes_.find(key);
                    if (!qualified) {
                        if (active != active_arb_episodes_.end()) {
                            emit_arb_episode(
                                arb_episode_ended_event_, "arb_episode_ended",
                                active->second, market_id, market, target_size,
                                delay_us, order, "qualification_ended");
                            active_arb_episodes_.erase(active);
                        }
                        continue;
                    }
                    if (active != active_arb_episodes_.end()) continue;
                    if (pending_arb_attempts_.size() >= max_pending_arb_attempts_)
                        continue;
                    const unsigned long long sequence = ++arb_observation_sequence_;
                    const std::string episode_id = run_id_ + ':' +
                        std::to_string(generation_) + ':' +
                        std::to_string(ws_session_id_) + ':' + market_id +
                        ":arb_episode:" + std::to_string(sequence);
                    active_arb_episodes_[key] = episode_id;
                    emit_arb_episode(
                        arb_episode_started_event_, "arb_episode_started",
                        episode_id, market_id, market, target_size, delay_us,
                        order, "post_cost_positive");
                    const bool first_is_up =
                        order == observed_arb::LegOrder::UP_THEN_DOWN;
                    const double now_us = steady_now_us();
                    const observed_arb::AttemptIdentity identity{
                        episode_id + ":attempt", market_id,
                        market.condition_id, generation_, ws_session_id_};
                    PendingArbObservation pending;
                    pending.attempt = observed_arb::start_buy_both(
                        identity, order, target_size, target_size, buffer,
                        first_is_up ? up : down, now_us, now_us + delay_us);
                    pending.episode_key = key;
                    pending.market_id = market_id;
                    pending.asset = market.asset;
                    pending.timeframe = market.interval;
                    pending.window = market.window;
                    pending.close_ts = market.close_ts;
                    pending.delay_us = delay_us;
                    pending.started_wall = timestamp;
                    pending.initial_up_vwap = up.vwap;
                    pending.initial_down_vwap = down.vwap;
                    pending.initial_up_fee = up.rounded_fee;
                    pending.initial_down_fee = down.rounded_fee;
                    pending.initial_net_cost = initial_net_cost;
                    pending.initial_locked_profit = initial_profit;
                    pending.first_is_up = first_is_up;
                    pending_arb_attempts_[identity.attempt_id] = pending;
                    emit_arb_observation(
                        arb_shadow_attempt_event_, "arb_shadow_attempt",
                        pending, nullptr, "attempt_started");
                    emit_arb_observation(
                        arb_shadow_leg_result_event_, "arb_shadow_leg_result",
                        pending, nullptr, "first_leg_book_executable", 1);
                    ++arb_attempts_started_;
                }
            }
        }
    }

    void process_due_arb_attempts() {
        const double now_us = steady_now_us();
        for (auto item = pending_arb_attempts_.begin();
                item != pending_arb_attempts_.end();) {
            PendingArbObservation& pending = item->second;
            if (now_us < pending.attempt.due_us) {
                ++item;
                continue;
            }
            observed_arb::Outcome outcome;
            auto market = markets_.find(pending.market_id);
            if (market == markets_.end()) {
                outcome.state = observed_arb::State::INVALIDATED;
                outcome.order = pending.attempt.order;
                outcome.reason = "market_removed";
                outcome.first_leg_book_executable = pending.attempt.valid;
                outcome.orphan_pnl = -pending.attempt.first_leg.gross_value -
                    pending.attempt.first_leg.rounded_fee -
                    pending.attempt.execution_buffer;
            } else {
                const Book& up_book = books_[market->second.up];
                const Book& down_book = books_[market->second.down];
                const double timestamp = now_seconds();
                const double up_age_ms = std::max(
                    0.0, (timestamp - up_book.updated_at) * 1000);
                const double down_age_ms = std::max(
                    0.0, (timestamp - down_book.updated_at) * 1000);
                const double feed_age_ms = std::max(
                    0.0, (timestamp - last_activity_) * 1000);
                const bool books_synced = std::min(
                    std::max(up_age_ms, down_age_ms), feed_age_ms) <= 750;
                const double rate = market->second.fee > 0
                    ? market->second.fee : fallback_fee_;
                const Book& second_book = pending.first_is_up
                    ? down_book : up_book;
                const Book& first_book = pending.first_is_up
                    ? up_book : down_book;
                outcome = observed_arb::observe_buy_both(
                    pending.attempt,
                    observed_buy_leg(
                        second_book, pending.attempt.target_size, rate,
                        books_synced),
                    observed_sell_leg(
                        first_book, pending.attempt.target_size, rate,
                        books_synced),
                    now_us);
            }
            emit_arb_observation(
                arb_shadow_leg_result_event_, "arb_shadow_leg_result",
                pending, &outcome, outcome.reason, 2);
            if (outcome.state == observed_arb::State::BOOK_EXECUTABLE) {
                emit_arb_observation(
                    arb_shadow_book_executable_event_,
                    "arb_shadow_book_executable", pending, &outcome,
                    outcome.reason);
                ++arb_book_executable_;
            } else if (outcome.state == observed_arb::State::ORPHANED) {
                emit_arb_observation(
                    arb_shadow_orphaned_event_, "arb_shadow_orphaned",
                    pending, &outcome, outcome.reason);
                ++arb_orphaned_;
            } else {
                emit_arb_observation(
                    arb_shadow_invalidated_event_, "arb_shadow_invalidated",
                    pending, &outcome, outcome.reason);
                ++arb_invalidated_;
            }
            item = pending_arb_attempts_.erase(item);
        }
        if (audit_ && now_seconds() - last_arb_summary_at_ >= 60) {
            last_arb_summary_at_ = now_seconds();
            audit_ << '{' << arb_research_summary_event_
                   << ",\"ts\":" << last_arb_summary_at_
                   << ",\"timestamp\":" << last_arb_summary_at_
                   << ",\"event_id\":\"" << run_id_ << ":arb_summary:"
                   << ++arb_observation_sequence_
                   << "\",\"strategy\":\"paired_lock\",\"active_episodes\":"
                   << active_arb_episodes_.size()
                   << ",\"pending_attempts\":" << pending_arb_attempts_.size()
                   << ",\"attempts\":" << arb_attempts_started_
                   << ",\"book_executable\":" << arb_book_executable_
                   << ",\"orphaned\":" << arb_orphaned_
                   << ",\"invalidated\":" << arb_invalidated_
                   << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n"
                   << std::flush;
        }
    }

    void invalidate_arb_attempts(const std::string& reason) {
        for (const auto& item : pending_arb_attempts_) {
            observed_arb::Outcome outcome;
            outcome.state = observed_arb::State::INVALIDATED;
            outcome.order = item.second.attempt.order;
            outcome.reason = reason;
            outcome.first_leg_book_executable = item.second.attempt.valid;
            outcome.orphan_pnl = -item.second.attempt.first_leg.gross_value -
                item.second.attempt.first_leg.rounded_fee -
                item.second.attempt.execution_buffer;
            emit_arb_observation(
                arb_shadow_invalidated_event_, "arb_shadow_invalidated",
                item.second, &outcome, reason);
            ++arb_invalidated_;
        }
        pending_arb_attempts_.clear();
        active_arb_episodes_.clear();
    }

    void emit_arbitrage_counterfactual(
            const std::string& market_id, Market& market,
            const Book& up_book, const Book& down_book, double rate,
            bool books_synced, double timestamp) {
        if (!audit_) return;
        const bool periodic_audit = timestamp - market.arb_research_last_audit >= 60;
        if (market.arb_research_initialized && !periodic_audit &&
                timestamp - market.arb_research_last_evaluated <
                    counterfactual_min_interval_seconds_) return;
        market.arb_research_last_evaluated = timestamp;
        if (market.arb_research_initialized &&
                up_book.version == market.arb_research_up_version &&
                down_book.version == market.arb_research_down_version &&
                books_synced == market.arb_research_books_synced &&
                !periodic_audit) return;

        struct StressSample {
            double delay_us = 0;
            complete_set::ExecutionStressResult result;
        };
        struct Observation {
            const char* method = nullptr;
            double target_size = 0;
            bool depth_ok = false;
            std::pair<double, double> up;
            std::pair<double, double> down;
            double total_fees = 0;
            double execution_buffer = 0;
            double post_cost_profit = 0;
            std::array<StressSample, 4> stress;
        };
        std::array<Observation, 8> observations;
        bool qualification_changed = false;
        size_t qualification_index = 0;
        size_t observation_index = 0;
        for (const double target_size : counterfactual_sizes_) {
            const auto up_buy = buy_vwap(up_book, target_size);
            const auto down_buy = buy_vwap(down_book, target_size);
            const auto up_sell = sell_vwap(up_book, target_size);
            const auto down_sell = sell_vwap(down_book, target_size);
            for (const bool buy_pair : {true, false}) {
                const auto up = buy_pair ? up_buy : up_sell;
                const auto down = buy_pair ? down_buy : down_sell;
                const double up_fee = std::round(
                    up.first * rate * up.second * (1 - up.second) * 100000
                ) / 100000;
                const double down_fee = std::round(
                    down.first * rate * down.second * (1 - down.second) * 100000
                ) / 100000;
                const double execution_buffer = target_size * (
                    buy_pair ? buffer_per_share_ : split_sell_buffer_per_share_
                );
                const double post_cost_profit = buy_pair
                    ? target_size - target_size * (up.second + down.second) -
                        up_fee - down_fee - execution_buffer
                    : target_size * (up.second + down.second) - target_size -
                        up_fee - down_fee - execution_buffer;
                const bool depth_ok = books_synced && up.first >= target_size &&
                    down.first >= target_size;
                auto& observation = observations[observation_index++];
                observation.method = buy_pair ? "paired_lock" : "split_sell_lock";
                observation.target_size = target_size;
                observation.depth_ok = depth_ok;
                observation.up = up;
                observation.down = down;
                observation.total_fees = up_fee + down_fee;
                observation.execution_buffer = execution_buffer;
                observation.post_cost_profit = post_cost_profit;
                for (size_t delay_index = 0;
                        delay_index < counterfactual_delays_us_.size(); ++delay_index) {
                    const double delay_us = counterfactual_delays_us_[delay_index];
                    const auto stress = complete_set::evaluate_execution_stress(
                        post_cost_profit,
                        target_size > 0 ? up.first / target_size : 0,
                        target_size > 0 ? down.first / target_size : 0,
                        delay_us, execution_half_life_us_,
                        target_size * orphan_loss_per_share_
                    );
                    const bool qualified = depth_ok && post_cost_profit > 0 &&
                        stress.expected_execution_value > 0;
                    if (!market.arb_research_initialized ||
                            market.arb_research_qualified[qualification_index] != qualified) {
                        qualification_changed = true;
                    }
                    market.arb_research_qualified[qualification_index++] = qualified;
                    observation.stress[delay_index] = {delay_us, stress};
                }
            }
        }
        market.arb_research_up_version = up_book.version;
        market.arb_research_down_version = down_book.version;
        market.arb_research_books_synced = books_synced;
        if (!qualification_changed && !periodic_audit) return;
        market.arb_research_initialized = true;
        market.arb_research_last_audit = timestamp;
        const unsigned long long sequence = ++evaluation_sequence_;
        std::ostringstream record;
        record << std::setprecision(15)
               << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
               << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':'
               << ws_session_id_ << ':' << market_id << ":arb_counterfactual:"
               << sequence << "\",\"event_type\":\"shadow_arb_counterfactual\""
               << ",\"strategy\":\"arbitrage_pattern_research\",\"market_id\":\""
               << market_id << "\",\"condition_id\":\""
               << reference_ipc::escaped(market.condition_id)
               << "\",\"asset\":\"" << reference_ipc::escaped(market.asset)
               << "\",\"timeframe\":\"" << reference_ipc::escaped(market.interval)
               << "\",\"window\":\"" << reference_ipc::escaped(market.window)
               << "\",\"close_ts\":" << market.close_ts
               << ",\"generation\":" << generation_ << ",\"session\":"
               << ws_session_id_ << ",\"research_only\":true,\"observations\":[";
        for (size_t index = 0; index < observation_index; ++index) {
            const auto& observation = observations[index];
            if (index > 0) record << ',';
            record << "{\"method\":\"" << observation.method
                   << "\",\"target_size\":" << observation.target_size
                   << ",\"depth_ok\":" << (observation.depth_ok ? "true" : "false")
                   << ",\"up_fill\":" << observation.up.first
                   << ",\"down_fill\":" << observation.down.first
                   << ",\"up_vwap\":" << observation.up.second
                   << ",\"down_vwap\":" << observation.down.second
                   << ",\"total_fees\":" << observation.total_fees
                   << ",\"execution_buffer\":" << observation.execution_buffer
                   << ",\"post_cost_profit\":" << observation.post_cost_profit
                   << ",\"latency_stress\":[";
            for (size_t delay_index = 0; delay_index < observation.stress.size();
                    ++delay_index) {
                const auto& sample = observation.stress[delay_index];
                if (delay_index > 0) record << ',';
                record << "{\"delay_ms\":" << sample.delay_us / 1000
                       << ",\"leg_1_fill_probability\":"
                       << sample.result.leg_1_fill_probability
                       << ",\"leg_2_fill_probability\":"
                       << sample.result.leg_2_fill_probability
                       << ",\"expected_execution_value\":"
                       << sample.result.expected_execution_value << '}';
            }
            record << "]}";
        }
        record << "],\"decision\":\"OBSERVED\",\"reason\":\"counterfactual_only\""
               << ",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n";
        queue_arb_audit(record.str());
    }

    microstructure_reversion::BookFill reversion_fill(
            const Book& book, double quantity, double rate, bool buy,
            double age_ms) const {
        const auto fill = buy ? buy_vwap(book, quantity) : sell_vwap(book, quantity);
        const double fee = std::round(
            fill.first * rate * fill.second * (1 - fill.second) * 100000
        ) / 100000;
        return {
            quantity, fill.first, fill.second, fill.first * fill.second, fee,
            age_ms, book.ws_snapshot, age_ms <= 750, crossed(book),
            book.generation, ws_session_id_,
        };
    }

    void emit_reversion_audit(
            const char* event_type, const std::string& market_id,
            const Market& market, const std::string& outcome,
            const std::string& decision, const std::string& reason,
            double timestamp, double anchor, double entry_vwap,
            double exit_vwap, double entry_cost, double net_exit,
            double net_profit) {
        const unsigned long long sequence = ++strategy_evaluation_sequence_;
        std::ostringstream out;
        out << std::setprecision(15)
            << "{\"ts\":" << timestamp << ",\"timestamp\":" << timestamp
            << ",\"event_id\":\"" << run_id_ << ':' << generation_ << ':'
            << ws_session_id_ << ':' << market_id << ":reversion:" << sequence
            << R"(","event_type":")" << event_type
            << R"(","strategy":"microstructure_reversion","market_id":")"
            << reference_ipc::escaped(market_id)
            << "\",\"condition_id\":\""
            << reference_ipc::escaped(market.condition_id)
            << "\",\"asset\":\"" << reference_ipc::escaped(market.asset)
            << "\",\"timeframe\":\"" << reference_ipc::escaped(market.interval)
            << "\",\"window\":\"" << reference_ipc::escaped(market.window)
            << "\",\"outcome\":\"" << reference_ipc::escaped(outcome)
            << "\",\"generation\":" << generation_ << ",\"session\":"
            << ws_session_id_ << ",\"evaluation_sequence\":" << sequence
            << ",\"target_size\":" << size_ << ",\"robust_anchor\":" << anchor
            << ",\"entry_vwap\":" << entry_vwap << ",\"exit_vwap\":"
            << exit_vwap << ",\"entry_total_cost\":" << entry_cost
            << ",\"net_exit_value\":" << net_exit << ",\"net_profit\":"
            << net_profit << ",\"decision\":\""
            << reference_ipc::escaped(decision) << "\",\"reason\":\""
            << reference_ipc::escaped(reason)
            << R"(","observation_semantics":"BOOK_EXECUTABLE_NOT_FILL")"
            << R"(,"reference_prices_used":false,"settlement_probability_used":false)"
            << R"(,"simulated_fill":false,"config_version":"microstructure-reversion-shadow-v1")"
            << ",\"config_hash\":\"" << reversion_strategy_config_hash_
            << R"(","real_order_submissions":0,"real_orders":0,"real_fills":0})"
            << '\n';
        if (!strategy_audit_.enqueue(out.str())) ++strategy_audit_backpressure_;
    }

    void evaluate_microstructure_reversion(
            const std::string& market_id, const Market& market,
            const std::string& outcome, const std::string& token,
            const Book& book, double rate, double age_ms,
            double seconds_to_close, double timestamp) {
        const double observed_us = steady_now_us();
        const double bid = best_bid(book), ask = best_ask(book);
        if (bid <= 0 || ask <= 0) return;
        auto& history = midpoint_history_[token];
        history.emplace_back(observed_us, (bid + ask) / 2);
        const double cutoff_us = observed_us - reversion_lookback_ms_ * 1000;
        while (!history.empty() && history.front().first < cutoff_us)
            history.pop_front();
        std::vector<double> midpoints;
        midpoints.reserve(history.size());
        for (const auto& sample : history) midpoints.push_back(sample.second);
        const double anchor = median(midpoints);
        const std::string key = market_id + "|" + outcome;
        const auto active = reversion_positions_.find(key);
        if (active != reversion_positions_.end()) {
            microstructure_reversion::ExitInput input;
            input.position = active->second;
            input.sell = reversion_fill(book, active->second.quantity, rate, false, age_ms);
            input.exit_execution_buffer =
                active->second.quantity * reversion_exit_buffer_per_share_;
            input.observed_us = observed_us;
            const auto result = microstructure_reversion::evaluate_exit(input);
            if (result.state == microstructure_reversion::State::HOLDING) return;
            const char* event_type = "shadow_reversion_no_exit";
            std::string decision = "NO_EXIT";
            if (result.state == microstructure_reversion::State::PROFIT_EXIT_BOOK_EXECUTABLE) {
                event_type = "shadow_reversion_exit_book_executable";
                decision = "EXIT_EXECUTABLE";
            } else if (result.state == microstructure_reversion::State::TIMEOUT_EXIT_BOOK_EXECUTABLE) {
                event_type = "shadow_reversion_timeout_exit_book_executable";
                decision = "EXIT_EXECUTABLE";
            } else if (result.state == microstructure_reversion::State::INVALIDATED) {
                event_type = "shadow_reversion_invalidated";
                decision = "INVALIDATED";
            }
            emit_reversion_audit(
                event_type, market_id, market, outcome, decision, result.reason,
                timestamp, active->second.robust_anchor,
                active->second.entry_vwap, input.sell.vwap,
                active->second.entry_total_cost, result.net_exit_value,
                result.net_profit);
            reversion_positions_.erase(active);
            return;
        }

        microstructure_reversion::EntryInput input;
        input.identity = {
            run_id_ + ':' + std::to_string(generation_) + ':' +
                std::to_string(ws_session_id_) + ':' + market_id + ':' + outcome,
            market_id, market.condition_id, token, generation_, ws_session_id_,
        };
        input.outcome = outcome;
        input.target_size = size_;
        input.robust_anchor = anchor;
        input.sample_count = history.size();
        input.sample_span_ms = history.size() > 1
            ? (history.back().first - history.front().first) / 1000 : 0;
        input.minimum_samples = reversion_minimum_samples_;
        input.minimum_sample_span_ms = reversion_minimum_sample_span_ms_;
        input.minimum_discount_per_share = reversion_minimum_discount_per_share_;
        input.maximum_spread = reversion_maximum_spread_;
        input.spread = ask - bid;
        input.seconds_to_close = seconds_to_close;
        input.maximum_holding_ms = reversion_maximum_holding_ms_;
        input.minimum_exit_margin_seconds = reversion_minimum_exit_margin_seconds_;
        input.entry_execution_buffer = size_ * reversion_entry_buffer_per_share_;
        input.minimum_profit = reversion_minimum_profit_;
        input.buy = reversion_fill(book, size_, rate, true, age_ms);
        input.observed_us = observed_us;
        const auto result = microstructure_reversion::evaluate_entry(input);
        const std::string fingerprint = result.reason;
        const auto previous = reversion_emission_state_.find(key);
        const bool periodic = previous == reversion_emission_state_.end() ||
            previous->second.first != fingerprint ||
            timestamp - previous->second.second >= 5;
        if (periodic) {
            emit_reversion_audit(
                "shadow_reversion_eval", market_id, market, outcome,
                result.state == microstructure_reversion::State::ENTRY_BOOK_EXECUTABLE
                    ? "ACCEPT" : "REJECT",
                result.reason, timestamp, anchor, input.buy.vwap, 0,
                result.position.entry_total_cost, 0, 0);
            reversion_emission_state_[key] = {fingerprint, timestamp};
        }
        if (result.state != microstructure_reversion::State::ENTRY_BOOK_EXECUTABLE)
            return;
        emit_reversion_audit(
            "shadow_reversion_candidate", market_id, market, outcome,
            "OBSERVED", result.reason, timestamp, anchor, input.buy.vwap, 0,
            result.position.entry_total_cost, 0, 0);
        emit_reversion_audit(
            "shadow_reversion_entry_book_executable", market_id, market,
            outcome, "ENTRY_EXECUTABLE", result.reason, timestamp, anchor,
            input.buy.vwap, 0, result.position.entry_total_cost, 0, 0);
        reversion_positions_[key] = result.position;
        record_session_strategy("microstructure_reversion", "ACCEPT");
    }

    void evaluate() {
        process_due_arb_attempts();
        for (auto& item : markets_) {
            Book& up_book = books_[item.second.up]; Book& down_book = books_[item.second.down];
            const double timestamp = now_seconds();
            bool crossed_book_pending = false;
            for (const auto& token : {item.second.up, item.second.down}) {
                Book& book = books_[token];
                if (!book.ws_snapshot || !crossed(book)) continue;
                crossed_book_pending = true;
                if (book.crossed_since == 0) book.crossed_since = timestamp;
                if (timestamp - book.crossed_since >= 0.5)
                    resync_token(token, "crossed_book");
            }
            if (crossed_book_pending) continue;
            if (!up_book.ws_snapshot || !down_book.ws_snapshot) {
                if (item.second.last_reason != "book_uninitialized" || timestamp - item.second.last_audit >= 5) {
                    std::cout << "SHADOW_EVAL\tmarket=" << item.first << "\treason=book_uninitialized\tfok=0\n" << std::flush;
                    item.second.last_reason = "book_uninitialized";
                    item.second.last_audit = timestamp;
                }
                continue;
            }
            const auto time_bucket = static_cast<unsigned long long>(
                timestamp / book_evaluation_interval_seconds_);
            const bool book_changed =
                up_book.version != item.second.last_book_evaluation_up_version ||
                down_book.version != item.second.last_book_evaluation_down_version;
            if (!book_changed &&
                    time_bucket == item.second.last_book_evaluation_time_bucket)
                continue;
            item.second.last_book_evaluation_up_version = up_book.version;
            item.second.last_book_evaluation_down_version = down_book.version;
            item.second.last_book_evaluation_time_bucket = time_bucket;
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
            const double up_best_ask = best_ask(up_book), down_best_ask = best_ask(down_book);
            const double rate = item.second.fee > 0 ? item.second.fee : fallback_fee_;
            evaluate_microstructure_reversion(
                item.first, item.second, "Up", item.second.up, up_book, rate,
                up_age_ms, seconds_to_close, timestamp);
            evaluate_microstructure_reversion(
                item.first, item.second, "Down", item.second.down, down_book, rate,
                down_age_ms, seconds_to_close, timestamp);
            emit_arbitrage_counterfactual(
                item.first, item.second, up_book, down_book, rate,
                books_synced, timestamp
            );
            update_observed_arbitrage(
                item.first, item.second, up_book, down_book, rate,
                books_synced, timestamp
            );
            sizing::PairedConfig paired_sizing_config;
            paired_sizing_config.shadow_capital_usd = shadow_sizing_capital_usd_;
            paired_sizing_config.maximum_capital_fraction = paired_max_capital_fraction_;
            paired_sizing_config.maximum_quantity = paired_max_quantity_;
            paired_sizing_config.minimum_order_size = item.second.min_order_size;
            paired_sizing_config.maximum_slippage_per_share = strategy_config_.maximum_slippage;
            paired_sizing_config.fee_rate = rate;
            paired_sizing_config.execution_buffer_per_share = buffer_per_share_;
            paired_sizing_config.minimum_locked_profit = min_profit_;
            paired_sizing_config.minimum_locked_roi = paired_min_locked_roi_;
            const auto sizing_result = sizing::size_paired_lock(
                up_book.asks, down_book.asks, paired_sizing_config);
            const double target_size = sizing_result.accepted
                ? sizing_result.dynamic_target_size : item.second.min_order_size;
            const auto up = buy_vwap(up_book, target_size);
            const auto down = buy_vwap(down_book, target_size);
            const bool fok = sizing_result.accepted &&
                up.first + 1e-9 >= target_size && down.first + 1e-9 >= target_size;
            const double up_fee = sizing_result.accepted
                ? sizing_result.up_fee
                : std::round(up.first * rate * up.second * (1 - up.second) * 100000) / 100000;
            const double down_fee = sizing_result.accepted
                ? sizing_result.down_fee
                : std::round(down.first * rate * down.second * (1 - down.second) * 100000) / 100000;
            const double gross_cost = target_size * (up.second + down.second);
            const double buffer = target_size * buffer_per_share_;
            const double net_cost = gross_cost + up_fee + down_fee + buffer;
            const double profit = fok ? target_size - net_cost : 0;
            const double leg_1_fill_probability = books_synced && target_size > 0
                ? std::min(1.0, up.first / target_size) : 0;
            const double latency_decay = std::exp(-leg_interval_us_ / std::max(1.0, execution_half_life_us_));
            const double leg_2_fill_probability = books_synced && target_size > 0
                ? std::min(1.0, down.first / target_size) * latency_decay : 0;
            const double orphan_leg_loss = target_size * orphan_loss_per_share_;
            const double both_fill_probability = leg_1_fill_probability * leg_2_fill_probability;
            const double expected_execution_value = both_fill_probability * profit - leg_1_fill_probability * (1 - leg_2_fill_probability) * orphan_leg_loss;
            const bool good = sizing_result.accepted && fok && books_synced && seconds_to_close >= 20 && seconds_to_close <= 7200
                              && profit >= min_profit_ && expected_execution_value >= min_expected_value_;
            const std::string reason = !books_synced ? "clob_book_stale"
                : seconds_to_close < 20 ? "closing_window"
                : seconds_to_close > 7200 ? "too_early"
                : !sizing_result.accepted ? sizing_result.reason
                : up.first < target_size ? "up_depth"
                : down.first < target_size ? "down_depth"
                : profit < min_profit_ ? "net_cost_above_threshold"
                : expected_execution_value < min_expected_value_ ? "execution_value_below_threshold"
                : "opportunity";
            const bool paired_was_active = item.second.active_since > 0;
            if (good && !paired_was_active) item.second.active_since = timestamp;
            if (!good) item.second.active_since = 0;
            if (reason != item.second.last_reason || timestamp - item.second.last_audit >= 5) {
                const unsigned long long evaluation_sequence = ++evaluation_sequence_;
                const std::string evaluation_id = run_id_ + ":" + std::to_string(generation_) + ":" + std::to_string(ws_session_id_) + ":" + item.first + ":" + std::to_string(evaluation_sequence);
                std::cout << "SHADOW_EVAL\tmarket=" << item.first << "\treason=" << reason
                          << "\tfok=" << (fok ? 1 : 0) << "\tup_fill=" << up.first << "\tdown_fill=" << down.first
                          << "\tup_vwap=" << up.second << "\tdown_vwap=" << down.second
                          << "\tfees=" << up_fee + down_fee << "\tnet_cost=" << net_cost << "\tlocked_profit=" << profit << "\n" << std::flush;
                if (audit_) audit_ << "{\"ts\":" << timestamp << ",\"event_id\":\"" << evaluation_id << "\",\"run_id\":\"" << run_id_ << "\",\"evaluation_sequence\":" << evaluation_sequence << ",\"event_type\":\"shadow_eval\",\"strategy\":\"paired_lock\",\"market_id\":\"" << item.first
                                   << "\",\"condition_id\":\"" << reference_ipc::escaped(item.second.condition_id)
                                   << "\",\"asset\":\"" << reference_ipc::escaped(item.second.asset)
                                   << "\",\"timeframe\":\"" << reference_ipc::escaped(item.second.interval)
                                   << "\",\"window\":\"" << reference_ipc::escaped(item.second.window)
                                   << "\",\"close_ts\":" << item.second.close_ts
                                   << ",\"generation\":" << generation_ << ",\"session\":" << ws_session_id_
                                   << ",\"reason\":\"" << reason << "\",\"fok\":" << (fok ? "true" : "false")
                                   << ",\"seconds_to_close\":" << seconds_to_close
                                   << ",\"size\":" << (sizing_result.accepted ? target_size : 0)
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
                                   << ",\"net_cost\":" << net_cost << ",\"guaranteed_payout\":" << (fok ? target_size : 0)
                                   << ",\"locked_profit\":" << profit << ",\"locked_roi\":" << (net_cost > 0 ? profit / net_cost : 0)
                                   << ",\"leg_1_fill_probability\":" << leg_1_fill_probability
                                   << ",\"leg_2_fill_probability\":" << leg_2_fill_probability
                                   << ",\"time_between_legs_us\":" << leg_interval_us_
                                   << ",\"orphan_leg_loss\":" << orphan_leg_loss
                                   << ",\"expected_execution_value\":" << expected_execution_value
                                   << ",\"execution_model\":\"configured_latency_stress\""
                                   << ",\"books_synced\":" << (books_synced ? "true" : "false")
                                   << ",\"sizing_mode\":\"real_market_dynamic_v1\""
                                   << ",\"requested_max_size\":" << sizing_result.requested_max_size
                                   << ",\"dynamic_target_size\":" << sizing_result.dynamic_target_size
                                   << ",\"market_minimum_size\":" << sizing_result.market_minimum_size
                                   << ",\"executable_depth_size\":" << sizing_result.executable_depth_size
                                   << ",\"slippage_limited_size\":" << sizing_result.slippage_limited_size
                                   << ",\"capital_limited_size\":" << sizing_result.capital_limited_size
                                   << ",\"shadow_capital_usd\":" << sizing_result.shadow_capital_usd
                                   << ",\"capital_budget_usd\":" << sizing_result.capital_budget_usd
                                   << ",\"input_quality_score\":null,\"conservative_probability\":null"
                                   << ",\"probability_haircut\":null,\"full_kelly_fraction\":null"
                                   << ",\"applied_kelly_fraction\":null"
                                   << ",\"dynamic_vwap\":null,\"dynamic_fee\":" << sizing_result.dynamic_fee
                                   << ",\"dynamic_buffer\":" << sizing_result.dynamic_buffer
                                   << ",\"dynamic_all_in_cost\":" << sizing_result.dynamic_all_in_cost
                                   << ",\"dynamic_all_in_price\":" << sizing_result.dynamic_all_in_price
                                   << ",\"dynamic_expected_profit\":" << sizing_result.dynamic_expected_profit
                                   << ",\"dynamic_maximum_loss\":" << sizing_result.dynamic_maximum_loss
                                   << ",\"size_binding_constraint\":";
                if (sizing_result.size_binding_constraint.empty()) audit_ << "null";
                else audit_ << '"' << reference_ipc::escaped(sizing_result.size_binding_constraint) << '"';
                audit_ << ",\"config_version\":\"paired-lock-shadow-v3\",\"config_hash\":\""
                                   << paired_config_hash_ << "\",\"decision\":\""
                                   << (good ? "ACCEPT" : "REJECT")
                                   << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n"
                                   << std::flush;
                record_session_strategy("paired_lock", good ? "ACCEPT" : "REJECT");
                item.second.last_reason = reason;
                item.second.last_audit = timestamp;
            }
            if (good) std::cout << "SHADOW_OPPORTUNITY\tmarket=" << item.first << "\tup_vwap=" << std::setprecision(12) << up.second
                                << "\tdown_vwap=" << down.second << "\tfees=" << up_fee + down_fee << "\tnet_cost=" << net_cost
                                << "\tprofit=" << profit << "\tfok=1\tduration_ms=" << (timestamp - item.second.active_since) * 1000 << "\n" << std::flush;
            if (good && !paired_was_active && audit_) {
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
                       << ",\"decision\":\"ACCEPT\",\"reason\":\"opportunity\",\"target_size\":" << target_size
                       << ",\"up_vwap\":" << up.second << ",\"down_vwap\":" << down.second
                       << ",\"up_cost\":" << target_size * up.second << ",\"down_cost\":" << target_size * down.second
                       << ",\"gross_cost\":" << gross_cost << ",\"up_fee\":" << up_fee << ",\"down_fee\":" << down_fee
                       << ",\"total_fees\":" << up_fee + down_fee << ",\"fee_rate\":" << rate
                       << ",\"execution_buffer\":" << buffer << ",\"buffer\":" << buffer
                       << ",\"net_cost\":" << net_cost << ",\"guaranteed_payout\":" << target_size
                       << ",\"locked_profit\":" << profit << ",\"locked_roi\":" << (net_cost > 0 ? profit / net_cost : 0)
                       << ",\"up_depth_ok\":" << (up.first >= target_size ? "true" : "false")
                       << ",\"down_depth_ok\":" << (down.first >= target_size ? "true" : "false")
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
                        << ",\"sizing_mode\":\"real_market_dynamic_v1\""
                        << ",\"requested_max_size\":" << sizing_result.requested_max_size
                        << ",\"dynamic_target_size\":" << target_size
                        << ",\"market_minimum_size\":" << sizing_result.market_minimum_size
                        << ",\"executable_depth_size\":" << sizing_result.executable_depth_size
                        << ",\"slippage_limited_size\":" << sizing_result.slippage_limited_size
                        << ",\"capital_limited_size\":" << sizing_result.capital_limited_size
                        << ",\"shadow_capital_usd\":" << sizing_result.shadow_capital_usd
                        << ",\"capital_budget_usd\":" << sizing_result.capital_budget_usd
                        << ",\"dynamic_fee\":" << sizing_result.dynamic_fee
                        << ",\"dynamic_buffer\":" << sizing_result.dynamic_buffer
                        << ",\"dynamic_all_in_cost\":" << sizing_result.dynamic_all_in_cost
                        << ",\"dynamic_all_in_price\":" << sizing_result.dynamic_all_in_price
                        << ",\"dynamic_expected_profit\":" << sizing_result.dynamic_expected_profit
                        << ",\"dynamic_maximum_loss\":" << sizing_result.dynamic_maximum_loss
                        << ",\"size_binding_constraint\":\""
                       << reference_ipc::escaped(sizing_result.size_binding_constraint) << "\""
                       << ",\"config_version\":\"paired-lock-shadow-v3\",\"config_hash\":\"" << paired_config_hash_
                       << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n" << std::flush;
            }

            const auto up_sell = sell_vwap(up_book, size_);
            const auto down_sell = sell_vwap(down_book, size_);
            const double up_sell_fee = std::round(
                up_sell.first * rate * up_sell.second * (1 - up_sell.second) * 100000
            ) / 100000;
            const double down_sell_fee = std::round(
                down_sell.first * rate * down_sell.second * (1 - down_sell.second) * 100000
            ) / 100000;
            const double split_sell_buffer = size_ * split_sell_buffer_per_share_;
            const double sell_leg_1_probability = books_synced
                ? std::min(1.0, up_sell.first / size_) : 0;
            const double sell_leg_2_probability = books_synced
                ? std::min(1.0, down_sell.first / size_) * latency_decay : 0;
            const auto split_sell = complete_set::evaluate_split_sell({
                size_, up_sell.first, down_sell.first,
                up_sell.second, down_sell.second,
                up_sell_fee, down_sell_fee, split_sell_buffer,
                min_profit_, min_expected_value_,
                sell_leg_1_probability, sell_leg_2_probability, orphan_leg_loss,
            });
            const bool split_sell_window = seconds_to_close >= 20 &&
                seconds_to_close <= 7200;
            const bool split_sell_good = books_synced && split_sell_window &&
                split_sell.decision == "ACCEPT";
            const std::string split_sell_reason = !books_synced
                ? "clob_book_stale"
                : seconds_to_close < 20
                    ? "closing_window"
                    : seconds_to_close > 7200
                        ? "too_early"
                        : split_sell.reason;
            const bool split_sell_was_active =
                item.second.split_sell_active_since > 0;
            if (split_sell_good && !split_sell_was_active)
                item.second.split_sell_active_since = timestamp;
            if (!split_sell_good) item.second.split_sell_active_since = 0;
            if (
                split_sell_reason != item.second.split_sell_last_reason ||
                timestamp - item.second.split_sell_last_audit >= 5
            ) {
                const unsigned long long sequence = ++evaluation_sequence_;
                const std::string event_id = run_id_ + ":" +
                    std::to_string(generation_) + ":" +
                    std::to_string(ws_session_id_) + ":" + item.first +
                    ":split_sell:" + std::to_string(sequence);
                if (audit_) {
                    audit_ << "{\"ts\":" << timestamp << ",\"timestamp\":"
                           << timestamp << ",\"event_id\":\"" << event_id
                           << "\",\"run_id\":\"" << run_id_
                           << "\",\"evaluation_sequence\":" << sequence
                           << ",\"event_type\":\"shadow_split_sell_eval\""
                           << ",\"strategy\":\"split_sell_lock\",\"arb_method\":\"SPLIT_AND_SELL_BOTH\""
                           << ",\"market_id\":\"" << item.first
                           << "\",\"condition_id\":\""
                           << reference_ipc::escaped(item.second.condition_id)
                           << "\",\"asset\":\""
                           << reference_ipc::escaped(item.second.asset)
                           << "\",\"timeframe\":\""
                           << reference_ipc::escaped(item.second.interval)
                           << "\",\"window\":\""
                           << reference_ipc::escaped(item.second.window)
                           << "\",\"generation\":" << generation_
                           << ",\"session\":" << ws_session_id_
                           << ",\"decision\":\""
                           << (split_sell_good ? "ACCEPT" : "REJECT")
                           << "\",\"reason\":\"" << split_sell_reason
                           << "\",\"target_size\":" << size_
                           << ",\"up_sell_fill\":" << up_sell.first
                           << ",\"down_sell_fill\":" << down_sell.first
                           << ",\"up_sell_vwap\":" << up_sell.second
                           << ",\"down_sell_vwap\":" << down_sell.second
                           << ",\"combined_bid_vwap\":"
                           << split_sell.combined_bid_vwap
                           << ",\"gross_proceeds\":" << split_sell.gross_proceeds
                           << ",\"up_fee\":" << up_sell_fee
                           << ",\"down_fee\":" << down_sell_fee
                           << ",\"total_fees\":" << split_sell.total_fees
                           << ",\"execution_buffer\":" << split_sell_buffer
                           << ",\"net_proceeds\":" << split_sell.net_proceeds
                           << ",\"split_collateral_cost\":"
                           << split_sell.collateral_cost
                           << ",\"locked_profit\":" << split_sell.locked_profit
                           << ",\"locked_roi\":" << split_sell.locked_roi
                           << ",\"observed_break_even_bid_sum\":"
                           << split_sell.observed_break_even_bid_sum
                           << ",\"observed_profit_threshold_bid_sum\":"
                           << split_sell.observed_profit_threshold_bid_sum
                           << ",\"profit_threshold_shortfall\":"
                           << split_sell.profit_threshold_shortfall
                           << ",\"required_gross_improvement_per_share\":"
                           << split_sell.required_gross_improvement_per_share
                           << ",\"required_gross_improvement_bps\":"
                           << split_sell.required_gross_improvement_bps
                           << ",\"expected_execution_value\":"
                           << split_sell.expected_execution_value
                           << ",\"leg_1_fill_probability\":"
                           << sell_leg_1_probability
                           << ",\"leg_2_fill_probability\":"
                           << sell_leg_2_probability
                           << ",\"time_between_legs_us\":" << leg_interval_us_
                           << ",\"orphan_leg_loss\":" << orphan_leg_loss
                           << ",\"inventory_assumption\":\"pre_split_complete_set_available\""
                           << ",\"books_ready\":true,\"books_fresh\":"
                           << (books_synced ? "true" : "false")
                           << ",\"books_synced\":"
                           << (books_synced ? "true" : "false")
                           << ",\"seconds_to_close\":" << seconds_to_close
                           << ",\"config_version\":\"split-sell-shadow-v2\""
                           << ",\"config_hash\":\""
                           << split_sell_strategy_config_hash_
                           << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n"
                           << std::flush;
                }
                record_session_strategy(
                    "split_sell_lock", split_sell_good ? "ACCEPT" : "REJECT");
                item.second.split_sell_last_reason = split_sell_reason;
                item.second.split_sell_last_audit = timestamp;
            }
            if (split_sell_good && !split_sell_was_active && audit_) {
                const unsigned long long sequence = ++opportunity_sequence_;
                audit_ << "{\"ts\":" << timestamp << ",\"timestamp\":"
                       << timestamp << ",\"event_id\":\"" << run_id_ << ':'
                       << generation_ << ':' << ws_session_id_ << ':' << item.first
                       << ":split_sell_opportunity:" << sequence
                       << "\",\"run_id\":\"" << run_id_
                       << "\",\"evaluation_sequence\":" << sequence
                       << ",\"event_type\":\"shadow_split_sell_opportunity\""
                       << ",\"strategy\":\"split_sell_lock\",\"arb_method\":\"SPLIT_AND_SELL_BOTH\""
                       << ",\"market_id\":\"" << item.first
                       << "\",\"condition_id\":\""
                       << reference_ipc::escaped(item.second.condition_id)
                       << "\",\"asset\":\""
                       << reference_ipc::escaped(item.second.asset)
                       << "\",\"timeframe\":\""
                       << reference_ipc::escaped(item.second.interval)
                       << "\",\"window\":\""
                       << reference_ipc::escaped(item.second.window)
                       << "\",\"generation\":" << generation_
                       << ",\"session\":" << ws_session_id_
                       << ",\"decision\":\"ACCEPT\",\"reason\":\"split_sell_opportunity\""
                       << ",\"target_size\":" << size_
                       << ",\"up_sell_fill\":" << up_sell.first
                       << ",\"down_sell_fill\":" << down_sell.first
                       << ",\"up_sell_vwap\":" << up_sell.second
                       << ",\"down_sell_vwap\":" << down_sell.second
                       << ",\"combined_bid_vwap\":"
                       << split_sell.combined_bid_vwap
                       << ",\"gross_proceeds\":" << split_sell.gross_proceeds
                       << ",\"up_fee\":" << up_sell_fee
                       << ",\"down_fee\":" << down_sell_fee
                       << ",\"total_fees\":" << split_sell.total_fees
                       << ",\"execution_buffer\":" << split_sell_buffer
                       << ",\"net_proceeds\":" << split_sell.net_proceeds
                       << ",\"split_collateral_cost\":"
                       << split_sell.collateral_cost
                       << ",\"locked_profit\":" << split_sell.locked_profit
                       << ",\"locked_roi\":" << split_sell.locked_roi
                       << ",\"observed_break_even_bid_sum\":"
                       << split_sell.observed_break_even_bid_sum
                       << ",\"observed_profit_threshold_bid_sum\":"
                       << split_sell.observed_profit_threshold_bid_sum
                       << ",\"profit_threshold_shortfall\":"
                       << split_sell.profit_threshold_shortfall
                       << ",\"required_gross_improvement_per_share\":"
                       << split_sell.required_gross_improvement_per_share
                       << ",\"required_gross_improvement_bps\":"
                       << split_sell.required_gross_improvement_bps
                       << ",\"expected_execution_value\":"
                       << split_sell.expected_execution_value
                       << ",\"leg_1_fill_probability\":"
                       << sell_leg_1_probability
                       << ",\"leg_2_fill_probability\":"
                       << sell_leg_2_probability
                       << ",\"time_between_legs_us\":" << leg_interval_us_
                       << ",\"orphan_leg_loss\":" << orphan_leg_loss
                       << ",\"inventory_assumption\":\"pre_split_complete_set_available\""
                       << ",\"books_ready\":true,\"books_fresh\":true,\"books_synced\":true"
                       << ",\"seconds_to_close\":" << seconds_to_close
                       << ",\"duration_ms\":"
                       << (timestamp - item.second.split_sell_active_since) * 1000
                       << ",\"config_version\":\"split-sell-shadow-v2\""
                       << ",\"config_hash\":\""
                       << split_sell_strategy_config_hash_
                       << "\",\"real_order_submissions\":0,\"real_orders\":0,\"real_fills\":0}\n"
                       << std::flush;
            }
        }
        process_due_arb_attempts();
        evaluate_reference_strategies();
        if (now_seconds() - last_health_write_ >= 1) write_health(true);
    }

    void record_session_strategy(
            const std::string& strategy_name, const std::string& decision) {
        auto& count = session_strategy_counts_[strategy_name];
        ++count.evaluations;
        if (decision == "ACCEPT") ++count.accepts;
        else ++count.rejections;
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
            << ",\"arb_audit_backpressure\":" << arb_audit_backpressure_
            << ",\"arb_audit_queue_depth\":" << arb_audit_queue_.size()
            << ",\"strategy_evaluations\":" << strategy_evaluation_sequence_
            << ",\"maker_quote_geometry_candidates\":"
            << maker_quote_geometry_candidates_
            << ",\"maker_trade_events\":" << maker_trade_events_
            << ",\"maker_single_leg_trade_throughs\":"
            << maker_single_leg_trade_throughs_
            << ",\"maker_both_leg_trade_throughs\":"
            << maker_both_leg_trade_throughs_
            << ",\"run_id\":\"" << run_id_ << "\""
            << ",\"engine_started_at\":" << engine_started_at_
            << ",\"paired_config_hash\":\"" << paired_config_hash_ << "\""
            << ",\"split_sell_config_hash\":\""
            << split_sell_strategy_config_hash_ << "\""
            << ",\"inventory_config_hash\":\"" << inventory_strategy_config_hash_ << "\""
            << ",\"maker_config_hash\":\"" << maker_strategy_config_hash_ << "\""
            << ",\"session_strategy_counts\":{";
        bool first_strategy = true;
        for (const auto& item : session_strategy_counts_) {
            if (!first_strategy) out << ',';
            first_strategy = false;
            out << '"' << item.first << "\":{\"evaluations\":"
                << item.second.evaluations << ",\"accepts\":" << item.second.accepts
                << ",\"rejections\":" << item.second.rejections << '}';
        }
        out << "},\"reference_receive_age_ms\":";
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
            << ",\"book_events\":" << book_events_
            << ",\"price_changes\":" << price_changes_
            << ",\"stale_price_changes_ignored\":"
            << stale_price_changes_ignored_ << "}\n";
        out.close();
        std::filesystem::rename(temporary, health_path_);
        last_health_write_ = now_seconds();
    }

    void schedule_ping() {
        timer_.expires_after(std::chrono::seconds(10));
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
            self->flush_arb_audit_queue();
            self->schedule_evaluation();
        });
    }

    void resync_token(const std::string& token, const std::string& reason) {
        auto found = books_.find(token);
        if (found == books_.end()) return;
        if (!found->second.ws_snapshot) return;
        found->second.ws_snapshot = false;
        found->second.bids.clear();
        found->second.asks.clear();
        invalidate_arb_attempts("book_resync");
        found->second.crossed_since = 0;
        for (auto& item : markets_) {
            if (item.second.up != token && item.second.down != token) continue;
            item.second.active_since = 0;
            item.second.split_sell_active_since = 0;
        }
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
                    item.second.last_audit = old->second.last_audit;
                    item.second.last_reason = old->second.last_reason;
                    item.second.split_sell_last_audit =
                        old->second.split_sell_last_audit;
                    item.second.split_sell_last_reason =
                        old->second.split_sell_last_reason;
                }
            }
            std::vector<std::string> added, removed;
            invalidate_arb_attempts("market_reload");
            ++generation_;
            maker_quote_observations_.clear();
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
        for (auto& item : books_) {
            item.second.ws_snapshot = false;
            item.second.bids.clear();
            item.second.asks.clear();
        }
        for (auto& item : markets_) {
            item.second.active_since = 0;
            item.second.split_sell_active_since = 0;
        }
        invalidate_arb_attempts("websocket_disconnected");
        flush_arb_audit_queue();
        maker_quote_observations_.clear();
        write_health(false);
        std::cerr << "WS_ERROR stage=" << stage << " code=" << ec.value() << " message=" << ec.message() << "\n";
    }

    static constexpr const char* arb_episode_started_event_ =
        "\"event_type\":\"arb_episode_started\"";
    static constexpr const char* arb_episode_ended_event_ =
        "\"event_type\":\"arb_episode_ended\"";
    static constexpr const char* arb_shadow_attempt_event_ =
        "\"event_type\":\"arb_shadow_attempt\"";
    static constexpr const char* arb_shadow_leg_result_event_ =
        "\"event_type\":\"arb_shadow_leg_result\"";
    static constexpr const char* arb_shadow_book_executable_event_ =
        "\"event_type\":\"arb_shadow_book_executable\"";
    static constexpr const char* arb_shadow_orphaned_event_ =
        "\"event_type\":\"arb_shadow_orphaned\"";
    static constexpr const char* arb_shadow_invalidated_event_ =
        "\"event_type\":\"arb_shadow_invalidated\"";
    static constexpr const char* arb_research_summary_event_ =
        "\"event_type\":\"arb_research_summary\"";
    static constexpr std::size_t max_pending_arb_attempts_ = 2048;
    static constexpr std::size_t max_arb_audit_queue_ = 4096;
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
    std::set<std::string> probability_observations_emitted_;
    std::map<std::string, double> probability_calibration_horizons_ = {
        {"5m", environment_double("MODEL_CALIBRATION_HORIZON_5M", "90")},
        {"15m", environment_double("MODEL_CALIBRATION_HORIZON_15M", "180")},
        {"1h", environment_double("MODEL_CALIBRATION_HORIZON_1H", "300")},
        {"4h", environment_double("MODEL_CALIBRATION_HORIZON_4H", "600")},
    };
    double size_, fallback_fee_, buffer_per_share_, min_profit_, leg_interval_us_, execution_half_life_us_;
    double orphan_loss_per_share_, min_expected_value_, last_activity_;
    double split_sell_buffer_per_share_ = environment_double(
        "SPLIT_SELL_BUFFER_PER_SHARE", "0.003");
    double profit_exit_buffer_per_share_ = environment_double(
        "SHADOW_PROFIT_EXIT_BUFFER_PER_SHARE", "0.001");
    double profit_exit_min_pnl_ = environment_double(
        "SHADOW_PROFIT_EXIT_MIN_PNL", "0.10");
    double reversion_lookback_ms_ = environment_double(
        "REVERSION_LOOKBACK_MS", "5000");
    double reversion_minimum_discount_per_share_ = environment_double(
        "REVERSION_MINIMUM_DISCOUNT_PER_SHARE", "0.02");
    double reversion_maximum_holding_ms_ = environment_double(
        "REVERSION_MAXIMUM_HOLDING_MS", "5000");
    double reversion_minimum_profit_ = environment_double(
        "REVERSION_MINIMUM_PROFIT", "0.10");
    double reversion_minimum_sample_span_ms_ = environment_double(
        "REVERSION_MINIMUM_SAMPLE_SPAN_MS", "2000");
    double reversion_maximum_spread_ = environment_double(
        "REVERSION_MAXIMUM_SPREAD", "0.05");
    double reversion_minimum_exit_margin_seconds_ = environment_double(
        "REVERSION_MINIMUM_EXIT_MARGIN_SECONDS", "10");
    double reversion_entry_buffer_per_share_ = environment_double(
        "REVERSION_ENTRY_BUFFER_PER_SHARE", "0.001");
    double reversion_exit_buffer_per_share_ = environment_double(
        "REVERSION_EXIT_BUFFER_PER_SHARE", "0.001");
    std::size_t reversion_minimum_samples_ = static_cast<std::size_t>(
        environment_double("REVERSION_MINIMUM_SAMPLES", "20"));
    unsigned long long book_events_ = 0, price_changes_ = 0;
    unsigned long long stale_price_changes_ignored_ = 0;
    bool stopped_ = false;
    std::ofstream audit_;
    BoundedAuditWriter strategy_audit_;
    std::string markets_path_, health_path_, run_id_;
    strategy::Config strategy_config_ = strategy_config_from_environment();
    double shadow_sizing_capital_usd_ = environment_double(
        "SHADOW_SIZING_CAPITAL_USD", "1000");
    double directional_fractional_kelly_ = environment_double(
        "DIRECTIONAL_FRACTIONAL_KELLY", "0.10");
    double directional_max_capital_fraction_ = environment_double(
        "DIRECTIONAL_MAX_CAPITAL_FRACTION", "0.02");
    double directional_probability_haircut_ = environment_double(
        "DIRECTIONAL_PROBABILITY_HAIRCUT", "0.02");
    double directional_max_quantity_ = environment_double(
        "DIRECTIONAL_MAX_QUANTITY", "100");
    double lottery_fractional_kelly_ = environment_double(
        "LOTTERY_FRACTIONAL_KELLY", "0.025");
    double lottery_max_capital_fraction_ = environment_double(
        "LOTTERY_MAX_CAPITAL_FRACTION", "0.005");
    double lottery_probability_haircut_ = environment_double(
        "LOTTERY_PROBABILITY_HAIRCUT", "0.05");
    double lottery_max_quantity_ = environment_double(
        "LOTTERY_MAX_QUANTITY", "100");
    double paired_max_capital_fraction_ = environment_double(
        "PAIRED_MAX_CAPITAL_FRACTION", "0.02");
    double paired_max_quantity_ = environment_double(
        "PAIRED_MAX_QUANTITY", "100");
    double paired_min_locked_roi_ = environment_double(
        "PAIRED_MIN_LOCKED_ROI", "0.001");
    std::map<std::string, complete_set::Inventory> complete_set_inventory_;
    std::map<std::string, std::string> active_arb_episodes_;
    std::map<std::string, PendingArbObservation> pending_arb_attempts_;
    std::deque<std::string> arb_audit_queue_;
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
    double inventory_legacy_max_guaranteed_loss_ = environment_double(
        "INVENTORY_LEGACY_MAX_GUARANTEED_LOSS", "0.50");
    double inventory_legacy_min_loss_reduction_ratio_ = environment_double(
        "INVENTORY_LEGACY_MIN_LOSS_REDUCTION_RATIO", "0.75");
    double maker_tick_size_ = environment_double("MAKER_TICK_SIZE", "0.01");
    double maker_quote_half_spread_ = environment_double("MAKER_QUOTE_HALF_SPREAD", "0.02");
    double maker_expected_rebate_per_pair_ = environment_double(
        "MAKER_EXPECTED_REBATE_PER_PAIR", "0");
    double maker_minimum_pair_edge_ = environment_double("MAKER_MINIMUM_PAIR_EDGE", "0.01");
    double maker_both_fill_probability_ = environment_double(
        "MAKER_BOTH_FILL_PROBABILITY", "0");
    double maker_orphan_loss_ = environment_double("MAKER_ORPHAN_LOSS", "0.02");
    double maker_observation_window_seconds_ = environment_double(
        "MAKER_OBSERVATION_WINDOW_SECONDS", "30");
    const std::array<double, 4> counterfactual_sizes_{{1, 2, 5, 10}};
    const std::array<double, 4> counterfactual_delays_us_{{0, 50000, 100000, 250000}};
    static constexpr double book_evaluation_interval_seconds_ = 0.25;
    static constexpr double counterfactual_min_interval_seconds_ = 0.1;
    std::string inventory_state_path_ = environment_value(
        "COMPLETE_SET_INVENTORY_STATE_PATH", "state/complete-set-inventory.json");
    std::string directional_strategy_config_hash_ = strategy_config_hash("late_window_directional_ev");
    std::string lottery_strategy_config_hash_ = strategy_config_hash("low_price_lottery_ev");
    std::string inventory_strategy_config_hash_ = strategy_config_hash("inventory_rebalancing_arb");
    std::string maker_strategy_config_hash_ = strategy_config_hash("maker_complete_set_arb");
    std::string paired_config_hash_, split_sell_strategy_config_hash_;
    std::map<std::string, std::pair<std::string, double>> strategy_emission_state_;
    std::map<std::string, SessionStrategyCount> session_strategy_counts_;
    std::map<std::string, MakerQuoteObservation> maker_quote_observations_;
    std::map<std::string, ProbabilityShadowPosition>
        active_probability_shadow_positions_;
    std::map<std::string, std::deque<std::pair<double, double>>> midpoint_history_;
    std::map<std::string, microstructure_reversion::Position> reversion_positions_;
    std::map<std::string, std::pair<std::string, double>> reversion_emission_state_;
    std::string reversion_strategy_config_hash_;
    double strategy_accept_heartbeat_seconds_ = 5, strategy_reject_heartbeat_seconds_ = 60;
    double last_health_write_ = 0;
    double engine_started_at_ = now_seconds();
    unsigned long long document_version_, generation_, ws_session_id_, full_resync_count_ = 0;
    unsigned long long evaluation_sequence_ = 0, opportunity_sequence_ = 0;
    unsigned long long strategy_evaluation_sequence_ = 0, strategy_audit_backpressure_ = 0;
    unsigned long long arb_audit_backpressure_ = 0;
    unsigned long long maker_quote_geometry_candidates_ = 0, maker_trade_events_ = 0;
    unsigned long long maker_single_leg_trade_throughs_ = 0;
    unsigned long long maker_both_leg_trade_throughs_ = 0;
    unsigned long long arb_observation_sequence_ = 0;
    unsigned long long arb_attempts_started_ = 0, arb_book_executable_ = 0;
    unsigned long long arb_orphaned_ = 0, arb_invalidated_ = 0;
    double last_arb_summary_at_ = 0;
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
