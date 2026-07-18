#pragma once

#include <algorithm>
#include <cmath>
#include <map>
#include <string>
#include <vector>

namespace sizing {

struct Result {
    bool accepted = false;
    std::string reason = "dynamic_depth_unavailable";
    std::string size_binding_constraint;
    double requested_max_size = 0;
    double dynamic_target_size = 0;
    double executable_depth_size = 0;
    double capital_limited_size = 0;
    double slippage_limited_size = 0;
    double market_minimum_size = 0;
    double shadow_capital_usd = 0;
    double capital_budget_usd = 0;
    double input_quality_score = 0;
    double estimated_probability = 0;
    double conservative_probability = 0;
    double probability_haircut = 0;
    double full_kelly_fraction = 0;
    double applied_kelly_fraction = 0;
    double dynamic_vwap = 0;
    double dynamic_buy_notional = 0;
    double dynamic_fee = 0;
    double dynamic_buffer = 0;
    double dynamic_all_in_cost = 0;
    double dynamic_all_in_price = 0;
    double dynamic_expected_profit = 0;
    double dynamic_maximum_loss = 0;
    double guaranteed_payout = 0;
    double locked_profit = 0;
    double locked_roi = 0;
    double up_vwap = 0;
    double down_vwap = 0;
    double up_notional = 0;
    double down_notional = 0;
    double up_fee = 0;
    double down_fee = 0;
};

struct ProbabilityConfig {
    double shadow_capital_usd = 0;
    double fractional_kelly = 0;
    double maximum_capital_fraction = 0;
    double probability_haircut = 0;
    double maximum_quantity = 0;
    double minimum_order_size = 0;
    double maximum_slippage_per_share = 0;
    double minimum_net_ev_per_share = 0;
    double fee_rate = 0;
    double execution_buffer_per_share = 0;
};

struct PairedConfig {
    double shadow_capital_usd = 0;
    double maximum_capital_fraction = 0;
    double maximum_quantity = 0;
    double minimum_order_size = 0;
    double maximum_slippage_per_share = 0;
    double fee_rate = 0;
    double execution_buffer_per_share = 0;
    double minimum_locked_profit = 0;
    double minimum_locked_roi = 0;
};

namespace detail {

struct Cost {
    bool complete = false;
    double quantity = 0;
    double notional = 0;
    double fee = 0;
    double vwap = 0;
    double buffer = 0;
    double total = 0;
    double unit = 0;
};

inline double round_fee(double value) {
    return std::round(std::max(0.0, value) * 100000.0) / 100000.0;
}

inline double available_depth(const std::map<double, double>& asks) {
    double depth = 0;
    for (const auto& level : asks) {
        if (std::isfinite(level.first) && std::isfinite(level.second) &&
                level.first > 0 && level.first < 1 && level.second > 0)
            depth += level.second;
    }
    return depth;
}

inline double best_ask(const std::map<double, double>& asks) {
    for (const auto& level : asks) {
        if (std::isfinite(level.first) && std::isfinite(level.second) &&
                level.first > 0 && level.first < 1 && level.second > 0)
            return level.first;
    }
    return 0;
}

inline Cost buy_cost(const std::map<double, double>& asks, double quantity,
                     double fee_rate, double buffer_per_share) {
    Cost result;
    if (!std::isfinite(quantity) || quantity <= 0) return result;
    double remaining = quantity;
    for (const auto& level : asks) {
        const double price = level.first;
        const double available = level.second;
        if (!std::isfinite(price) || !std::isfinite(available) ||
                price <= 0 || price >= 1 || available <= 0)
            continue;
        const double take = std::min(remaining, available);
        result.quantity += take;
        result.notional += take * price;
        result.fee += round_fee(take * fee_rate * price * (1 - price));
        remaining -= take;
        if (remaining <= 1e-9) break;
    }
    result.complete = remaining <= 1e-8;
    if (!result.complete || result.quantity <= 0) return result;
    result.vwap = result.notional / result.quantity;
    result.buffer = result.quantity * buffer_per_share;
    result.total = result.notional + result.fee + result.buffer;
    result.unit = result.total / result.quantity;
    return result;
}

template <typename Predicate>
inline double largest_valid(double minimum, double maximum, Predicate valid) {
    if (maximum < minimum || !valid(minimum)) return 0;
    if (valid(maximum)) return maximum;
    double low = minimum, high = maximum;
    for (int index = 0; index < 48; ++index) {
        const double middle = (low + high) / 2;
        if (valid(middle)) low = middle;
        else high = middle;
    }
    return low;
}

inline void copy_cost(Result& result, const Cost& cost) {
    result.dynamic_target_size = cost.quantity;
    result.dynamic_vwap = cost.vwap;
    result.dynamic_buy_notional = cost.notional;
    result.dynamic_fee = cost.fee;
    result.dynamic_buffer = cost.buffer;
    result.dynamic_all_in_cost = cost.total;
    result.dynamic_all_in_price = cost.unit;
    result.dynamic_maximum_loss = cost.total;
}

}  // namespace detail

inline Result size_probability_position(
        const std::map<double, double>& asks, double estimated_probability,
        double input_quality_score, const ProbabilityConfig& config) {
    Result result;
    result.requested_max_size = config.maximum_quantity;
    result.market_minimum_size = config.minimum_order_size;
    result.shadow_capital_usd = config.shadow_capital_usd;
    result.estimated_probability = estimated_probability;
    result.input_quality_score = input_quality_score;
    result.probability_haircut = config.probability_haircut;
    if (!std::isfinite(config.shadow_capital_usd) || config.shadow_capital_usd <= 0 ||
            !std::isfinite(config.maximum_capital_fraction) || config.maximum_capital_fraction <= 0 ||
            !std::isfinite(config.fractional_kelly) || config.fractional_kelly <= 0) {
        result.reason = "sizing_capital_unavailable";
        return result;
    }
    if (!std::isfinite(estimated_probability) || !std::isfinite(input_quality_score)) {
        result.reason = "sizing_probability_unavailable";
        return result;
    }
    const double ask = detail::best_ask(asks);
    const double depth = detail::available_depth(asks);
    if (ask <= 0 || depth <= 0) {
        result.reason = "dynamic_depth_unavailable";
        return result;
    }
    result.executable_depth_size = std::min(depth, config.maximum_quantity);
    if (result.executable_depth_size + 1e-9 < config.minimum_order_size) {
        result.reason = "dynamic_size_below_market_minimum";
        return result;
    }
    const double quality = std::clamp(input_quality_score, 0.0, 1.0);
    result.conservative_probability = std::clamp(
        ask + quality * (estimated_probability - ask) - config.probability_haircut,
        0.0, 1.0);
    const auto slippage_valid = [&](double quantity) {
        const auto cost = detail::buy_cost(asks, quantity, config.fee_rate,
                                           config.execution_buffer_per_share);
        return cost.complete && cost.vwap - ask <= config.maximum_slippage_per_share + 1e-12;
    };
    result.slippage_limited_size = detail::largest_valid(
        config.minimum_order_size, result.executable_depth_size, slippage_valid);
    if (result.slippage_limited_size <= 0) {
        result.reason = "dynamic_size_below_market_minimum";
        return result;
    }
    const auto qualifies = [&](double quantity) {
        const auto cost = detail::buy_cost(asks, quantity, config.fee_rate,
                                           config.execution_buffer_per_share);
        if (!cost.complete || cost.unit >= 1) return false;
        const double edge = result.conservative_probability - cost.unit;
        if (edge + 1e-12 < config.minimum_net_ev_per_share) return false;
        const double full_kelly = std::max(
            0.0, (result.conservative_probability - cost.unit) / (1 - cost.unit));
        const double applied = std::min(
            config.maximum_capital_fraction, config.fractional_kelly * full_kelly);
        return cost.total <= config.shadow_capital_usd * applied + 1e-9;
    };
    const auto minimum_cost = detail::buy_cost(
        asks, config.minimum_order_size, config.fee_rate,
        config.execution_buffer_per_share);
    if (result.conservative_probability - minimum_cost.unit + 1e-12 <
            config.minimum_net_ev_per_share) {
        result.reason = "net_ev_threshold";
        return result;
    }
    const double quantity = detail::largest_valid(
        config.minimum_order_size, result.slippage_limited_size, qualifies);
    if (quantity <= 0) {
        result.reason = "dynamic_size_below_market_minimum";
        return result;
    }
    const auto cost = detail::buy_cost(
        asks, quantity, config.fee_rate, config.execution_buffer_per_share);
    detail::copy_cost(result, cost);
    result.full_kelly_fraction = std::max(
        0.0, (result.conservative_probability - cost.unit) / (1 - cost.unit));
    result.applied_kelly_fraction = std::min(
        config.maximum_capital_fraction,
        config.fractional_kelly * result.full_kelly_fraction);
    result.capital_budget_usd = config.shadow_capital_usd * result.applied_kelly_fraction;
    result.capital_limited_size = quantity;
    result.dynamic_expected_profit =
        quantity * result.conservative_probability - cost.total;
    if (quantity >= result.executable_depth_size - 1e-6 && depth <= config.maximum_quantity + 1e-6)
        result.size_binding_constraint = "executable_depth";
    else if (quantity >= config.maximum_quantity - 1e-6)
        result.size_binding_constraint = "strategy_quantity_cap";
    else if (quantity >= result.slippage_limited_size - 1e-6 &&
             result.slippage_limited_size < result.executable_depth_size - 1e-6)
        result.size_binding_constraint = "slippage_limit";
    else
        result.size_binding_constraint = "capital_budget";
    result.accepted = true;
    result.reason = "dynamic_size_available";
    return result;
}

inline Result size_paired_lock(
        const std::map<double, double>& up_asks,
        const std::map<double, double>& down_asks,
        const PairedConfig& config) {
    Result result;
    result.requested_max_size = config.maximum_quantity;
    result.market_minimum_size = config.minimum_order_size;
    result.shadow_capital_usd = config.shadow_capital_usd;
    result.capital_budget_usd = config.shadow_capital_usd * config.maximum_capital_fraction;
    if (!std::isfinite(config.shadow_capital_usd) || config.shadow_capital_usd <= 0 ||
            !std::isfinite(config.maximum_capital_fraction) || config.maximum_capital_fraction <= 0) {
        result.reason = "sizing_capital_unavailable";
        return result;
    }
    const double up_depth = detail::available_depth(up_asks);
    const double down_depth = detail::available_depth(down_asks);
    result.executable_depth_size = std::min({up_depth, down_depth, config.maximum_quantity});
    if (result.executable_depth_size + 1e-9 < config.minimum_order_size) {
        result.reason = result.executable_depth_size <= 0
            ? "dynamic_depth_unavailable" : "dynamic_size_below_market_minimum";
        return result;
    }
    const double up_ask = detail::best_ask(up_asks);
    const double down_ask = detail::best_ask(down_asks);
    const auto slippage_valid = [&](double quantity) {
        const auto up = detail::buy_cost(up_asks, quantity, config.fee_rate, 0);
        const auto down = detail::buy_cost(down_asks, quantity, config.fee_rate, 0);
        return up.complete && down.complete &&
            up.vwap - up_ask <= config.maximum_slippage_per_share + 1e-12 &&
            down.vwap - down_ask <= config.maximum_slippage_per_share + 1e-12;
    };
    result.slippage_limited_size = detail::largest_valid(
        config.minimum_order_size, result.executable_depth_size, slippage_valid);
    if (result.slippage_limited_size <= 0) {
        result.reason = "dynamic_size_below_market_minimum";
        return result;
    }
    const auto economic_limits_pass = [&](double quantity) {
        const auto up = detail::buy_cost(up_asks, quantity, config.fee_rate, 0);
        const auto down = detail::buy_cost(down_asks, quantity, config.fee_rate, 0);
        if (!up.complete || !down.complete) return false;
        const double buffer = quantity * config.execution_buffer_per_share;
        const double net_cost = up.total + down.total + buffer;
        const double profit = quantity - net_cost;
        const double roi = net_cost > 0 ? profit / net_cost : 0;
        return net_cost <= result.capital_budget_usd + 1e-9 &&
            roi + 1e-12 >= config.minimum_locked_roi;
    };
    const double economic_quantity = detail::largest_valid(
        config.minimum_order_size, result.slippage_limited_size, economic_limits_pass);
    if (economic_quantity <= 0) {
        const auto up = detail::buy_cost(
            up_asks, config.minimum_order_size, config.fee_rate, 0);
        const auto down = detail::buy_cost(
            down_asks, config.minimum_order_size, config.fee_rate, 0);
        const double minimum_net_cost = up.total + down.total +
            config.minimum_order_size * config.execution_buffer_per_share;
        result.reason = minimum_net_cost >= config.minimum_order_size
            ? "net_cost_above_threshold" : "locked_roi_below_threshold";
        return result;
    }
    const auto profit_at = [&](double quantity) {
        const auto up = detail::buy_cost(up_asks, quantity, config.fee_rate, 0);
        const auto down = detail::buy_cost(down_asks, quantity, config.fee_rate, 0);
        return quantity - up.total - down.total -
            quantity * config.execution_buffer_per_share;
    };
    double quantity = 0;
    if (profit_at(economic_quantity) + 1e-12 >= config.minimum_locked_profit) {
        quantity = economic_quantity;
    } else {
        std::vector<double> candidates{config.minimum_order_size, economic_quantity};
        double cumulative = 0;
        for (const auto& level : up_asks) {
            if (level.second > 0) {
                cumulative += level.second;
                if (cumulative >= config.minimum_order_size && cumulative <= economic_quantity)
                    candidates.push_back(cumulative);
            }
        }
        cumulative = 0;
        for (const auto& level : down_asks) {
            if (level.second > 0) {
                cumulative += level.second;
                if (cumulative >= config.minimum_order_size && cumulative <= economic_quantity)
                    candidates.push_back(cumulative);
            }
        }
        for (const double candidate : candidates) {
            if (economic_limits_pass(candidate) &&
                    profit_at(candidate) + 1e-12 >= config.minimum_locked_profit)
                quantity = std::max(quantity, candidate);
        }
    }
    if (quantity <= 0) {
        result.reason = "locked_profit_below_threshold";
        return result;
    }
    const auto up = detail::buy_cost(up_asks, quantity, config.fee_rate, 0);
    const auto down = detail::buy_cost(down_asks, quantity, config.fee_rate, 0);
    result.dynamic_target_size = quantity;
    result.up_vwap = up.vwap;
    result.down_vwap = down.vwap;
    result.up_notional = up.notional;
    result.down_notional = down.notional;
    result.up_fee = up.fee;
    result.down_fee = down.fee;
    result.dynamic_buy_notional = result.up_notional + result.down_notional;
    result.dynamic_fee = result.up_fee + result.down_fee;
    result.dynamic_buffer = quantity * config.execution_buffer_per_share;
    result.dynamic_all_in_cost = result.dynamic_buy_notional + result.dynamic_fee + result.dynamic_buffer;
    result.dynamic_all_in_price = result.dynamic_all_in_cost / quantity;
    result.dynamic_maximum_loss = result.dynamic_all_in_cost;
    result.guaranteed_payout = quantity;
    result.locked_profit = quantity - result.dynamic_all_in_cost;
    result.locked_roi = result.locked_profit / result.dynamic_all_in_cost;
    result.dynamic_expected_profit = result.locked_profit;
    result.capital_limited_size = quantity;
    if (quantity >= result.executable_depth_size - 1e-6 &&
            std::min(up_depth, down_depth) <= config.maximum_quantity + 1e-6)
        result.size_binding_constraint = "executable_depth";
    else if (quantity >= config.maximum_quantity - 1e-6)
        result.size_binding_constraint = "strategy_quantity_cap";
    else if (quantity >= result.slippage_limited_size - 1e-6 &&
             result.slippage_limited_size < result.executable_depth_size - 1e-6)
        result.size_binding_constraint = "slippage_limit";
    else
        result.size_binding_constraint = "capital_budget";
    result.accepted = true;
    result.reason = "dynamic_size_available";
    return result;
}

}  // namespace sizing
