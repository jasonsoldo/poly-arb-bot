#pragma once

#include <algorithm>
#include <cmath>
#include <map>
#include <optional>
#include <string>
#include <vector>

namespace strategy {

struct Config {
    double directional_min_net_ev = .015;
    double directional_latency_buffer = .003;
    double directional_settlement_buffer = .002;
    double lottery_min_price = .01;
    double lottery_max_price = .05;
    double lottery_min_net_ev = .015;
    double lottery_model_buffer = .01;
    double lottery_execution_buffer = .005;
    double minimum_liquidity = 20;
    double maximum_slippage = .01;
    double maximum_reference_age_ms = 3000;
    double maximum_book_age_ms = 750;
    double maximum_clock_skew_ms = 250;
    double momentum_z_per_bps = .002;
    double imbalance_z = .25;
    double minimum_model_sample_span_seconds = 60;
};

struct ProbabilityInput {
    std::optional<double> consensus_price;
    std::optional<double> price_to_beat;
    double seconds_to_close = 0;
    std::optional<double> volatility_per_sqrt_second;
    int model_sample_count = 0;
    double model_sample_span_seconds = 0;
    std::optional<double> momentum_bps_30s;
    std::optional<double> paired_book_imbalance;
};

struct ProbabilityOutput {
    std::optional<double> estimated_probability;
    std::optional<double> expected_move_log_std;
    std::optional<double> reference_log_distance;
    std::optional<double> up_standardized_distance;
    std::optional<double> up_momentum_z;
    std::optional<double> up_imbalance_z;
    std::optional<double> up_final_model_z;
};

inline ProbabilityOutput probability_model(const ProbabilityInput& row, const Config& config = {}) {
    ProbabilityOutput output;
    if (!row.consensus_price || *row.consensus_price == 0 ||
        !row.price_to_beat || *row.price_to_beat == 0 ||
        !row.volatility_per_sqrt_second || *row.volatility_per_sqrt_second == 0 ||
        row.model_sample_count < 20 ||
        row.model_sample_span_seconds < config.minimum_model_sample_span_seconds ||
        row.seconds_to_close <= 0 || !row.momentum_bps_30s || !row.paired_book_imbalance) {
        return output;
    }
    const double scale = *row.volatility_per_sqrt_second * std::sqrt(row.seconds_to_close);
    if (scale <= 0) return output;
    const double log_distance = std::log(*row.consensus_price / *row.price_to_beat);
    const double standardized = log_distance / scale;
    const double momentum_z = *row.momentum_bps_30s * config.momentum_z_per_bps;
    const double imbalance_z = *row.paired_book_imbalance * config.imbalance_z;
    const double final_z = standardized + momentum_z + imbalance_z;
    output.estimated_probability = std::clamp(
        .5 * (1 + std::erf(final_z / std::sqrt(2.0))), .001, .999);
    output.expected_move_log_std = scale;
    output.reference_log_distance = log_distance;
    output.up_standardized_distance = standardized;
    output.up_momentum_z = momentum_z;
    output.up_imbalance_z = imbalance_z;
    output.up_final_model_z = final_z;
    return output;
}

struct EvaluationInput {
    std::string strategy;
    std::string timeframe;
    double expected_fill_price = 0;
    std::optional<double> estimated_probability;
    int seconds_to_close = 0;
    std::optional<double> price_to_beat = 100;
    double fee_per_share = .01;
    double slippage_per_share = .002;
    double liquidity = 100;
    double book_age_ms = 50;
    std::optional<double> reference_age_ms = 50;
    std::optional<double> clock_skew_ms = 10;
    bool market_active = true;
    bool market_tradable = true;
    bool target_depth_ok = true;
    std::optional<double> momentum_bps_30s = 2;
    std::optional<double> order_book_imbalance = .1;
    bool reference_quorum_met = true;
    std::string reference_block_reason;
    bool settlement_source_verified = true;
    std::string probability_block_reason;
};

struct Decision {
    std::string strategy;
    std::optional<double> gross_edge;
    std::optional<double> net_ev;
    std::string decision;
    std::string reason;
    std::vector<std::string> blocking_reasons;
};

inline void append_reason(std::vector<std::string>& reasons, const std::string& reason) {
    if (!reason.empty() && std::find(reasons.begin(), reasons.end(), reason) == reasons.end())
        reasons.push_back(reason);
}

inline std::vector<std::string> common_rejections(const EvaluationInput& row, const Config& config) {
    std::vector<std::string> reasons;
    if (!row.market_active || !row.market_tradable) append_reason(reasons, "market_not_tradable");
    if (row.book_age_ms > config.maximum_book_age_ms) append_reason(reasons, "clob_book_stale");
    if (!row.clock_skew_ms) append_reason(reasons, "clock_skew_unavailable");
    else if (std::abs(*row.clock_skew_ms) > config.maximum_clock_skew_ms)
        append_reason(reasons, "clock_skew_exceeded");
    if (!row.reference_quorum_met) append_reason(
        reasons, row.reference_block_reason.empty()
            ? "insufficient_reference_sources" : row.reference_block_reason);
    if (!row.settlement_source_verified) append_reason(reasons, "settlement_reference_unverified");
    if (!row.reference_age_ms || *row.reference_age_ms > config.maximum_reference_age_ms)
        append_reason(reasons, "reference_data_stale");
    if (!row.price_to_beat) append_reason(
        reasons, row.probability_block_reason.empty() ? "price_to_beat_missing" : row.probability_block_reason);
    else if (!row.estimated_probability) append_reason(
        reasons, row.probability_block_reason.empty() ? "probability_model_unavailable" : row.probability_block_reason);
    if (row.liquidity < config.minimum_liquidity) append_reason(reasons, "insufficient_liquidity");
    if (!row.target_depth_ok) append_reason(reasons, "target_depth_insufficient");
    if (row.slippage_per_share > config.maximum_slippage) append_reason(reasons, "slippage_exceeded");
    if (!row.momentum_bps_30s) append_reason(reasons, "momentum_unavailable");
    if (!row.order_book_imbalance) append_reason(reasons, "order_book_imbalance_unavailable");
    return reasons;
}

inline std::optional<std::pair<int, int>> directional_window(const std::string& timeframe) {
    static const std::map<std::string, std::pair<int, int>> windows = {
        {"5m", {15, 90}}, {"15m", {20, 180}}, {"1h", {30, 300}}, {"4h", {60, 600}},
    };
    const auto found = windows.find(timeframe);
    return found == windows.end() ? std::nullopt : std::optional(found->second);
}

inline Decision evaluate_directional(const EvaluationInput& row, const Config& config = {}) {
    auto reasons = common_rejections(row, config);
    const auto window = directional_window(row.timeframe);
    if (!window || row.seconds_to_close < window->first || row.seconds_to_close > window->second)
        append_reason(reasons, "outside_time_window");
    std::optional<double> gross;
    std::optional<double> net;
    if (row.estimated_probability) {
        gross = *row.estimated_probability - row.expected_fill_price;
        net = *gross - row.fee_per_share - row.slippage_per_share -
              config.directional_latency_buffer - config.directional_settlement_buffer;
        if (*net < config.directional_min_net_ev) append_reason(reasons, "net_ev_below_threshold");
    }
    return {"late_window_directional_ev", gross, net,
            reasons.empty() ? "ACCEPT" : "REJECT",
            reasons.empty() ? "positive_net_ev" : reasons.front(), std::move(reasons)};
}

inline Decision evaluate_lottery(const EvaluationInput& row, const Config& config = {}) {
    auto reasons = common_rejections(row, config);
    if (row.expected_fill_price < config.lottery_min_price || row.expected_fill_price > config.lottery_max_price)
        append_reason(reasons, row.expected_fill_price > config.lottery_max_price
            ? "entry_price_above_limit" : "entry_price_below_limit");
    std::optional<double> gross;
    std::optional<double> net;
    if (row.estimated_probability) {
        gross = *row.estimated_probability - row.expected_fill_price;
        net = *gross - row.fee_per_share - row.slippage_per_share -
              config.lottery_model_buffer - config.lottery_execution_buffer;
        if (*net < config.lottery_min_net_ev) append_reason(reasons, "net_ev_below_threshold");
    }
    return {"low_price_lottery_ev", gross, net,
            reasons.empty() ? "ACCEPT" : "REJECT",
            reasons.empty() ? "positive_net_ev" : reasons.front(), std::move(reasons)};
}

}  // namespace strategy
