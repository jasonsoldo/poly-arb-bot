#pragma once

#include <algorithm>
#include <cmath>
#include <string>

namespace complete_set {

struct Inventory {
    double up_quantity = 0;
    double down_quantity = 0;
    double up_cost = 0;
    double down_cost = 0;
};

struct RebalanceInput {
    Inventory inventory;
    double target_size = 0;
    double up_probability = 0;
    double up_unit_cost = 0;
    double down_unit_cost = 0;
    double up_depth = 0;
    double down_depth = 0;
    double minimum_entry_edge = .05;
    double minimum_entry_ev_roi = .25;
    double maximum_initial_price = .20;
    double maximum_complement_gap = .03;
    double minimum_locked_profit = .01;
    double minimum_locked_roi = .02;
    double maximum_unmatched_notional = .50;
    bool allow_loss_cap = false;
    double maximum_loss_cap = .50;
    double minimum_loss_reduction_ratio = .75;
};

struct RebalanceDecision {
    std::string decision = "REJECT";
    std::string reason = "no_inventory_edge";
    std::string action = "HOLD";
    std::string outcome;
    double quantity = 0;
    double unit_cost = 0;
    double probability = 0;
    double probability_edge = 0;
    double expected_value = 0;
    double expected_value_roi = 0;
    double maximum_loss = 0;
    double complement_gap = 0;
    double projected_locked_quantity = 0;
    double projected_locked_profit = 0;
    double projected_locked_roi = 0;
    double guaranteed_loss = 0;
    double loss_reduction_ratio = 0;
    double projected_residual_quantity = 0;
};

inline RebalanceDecision evaluate_rebalance(const RebalanceInput& row) {
    RebalanceDecision result;
    const double unmatched_up = std::max(0.0, row.inventory.up_quantity - row.inventory.down_quantity);
    const double unmatched_down = std::max(0.0, row.inventory.down_quantity - row.inventory.up_quantity);

    if (unmatched_up > 0) {
        const double quantity = std::min({unmatched_up, row.target_size, row.down_depth});
        const double average_up_cost = row.inventory.up_cost /
            std::max(row.inventory.up_quantity, 1e-12);
        result.outcome = "Down";
        result.quantity = quantity;
        result.unit_cost = row.down_unit_cost;
        result.projected_locked_quantity = quantity;
        result.projected_locked_profit = quantity * (1 - average_up_cost - row.down_unit_cost);
        const double pair_cost = quantity * (average_up_cost + row.down_unit_cost);
        result.projected_locked_roi = pair_cost > 0
            ? result.projected_locked_profit / pair_cost : 0;
        const double held_cost = quantity * average_up_cost;
        result.guaranteed_loss = std::max(0.0, -result.projected_locked_profit);
        result.loss_reduction_ratio = held_cost > 0
            ? 1 - result.guaranteed_loss / held_cost : 0;
        result.projected_residual_quantity = unmatched_up - quantity;
        if (quantity <= 0) result.reason = "down_depth";
        else if (
            result.projected_locked_profit >= row.minimum_locked_profit
            && result.projected_locked_roi >= row.minimum_locked_roi
        ) {
            result.decision = "ACCEPT";
            result.reason = "inventory_lock";
            result.action = "BUY_DOWN_AND_LOCK";
        } else if (
            row.allow_loss_cap
            && result.guaranteed_loss <= row.maximum_loss_cap
            && result.loss_reduction_ratio >= row.minimum_loss_reduction_ratio
        ) {
            result.decision = "ACCEPT";
            result.reason = "legacy_inventory_loss_cap";
            result.action = "BUY_DOWN_AND_CAP_LOSS";
        } else if (result.projected_locked_profit < row.minimum_locked_profit) {
            result.reason = "complement_cost_above_lock_threshold";
        } else {
            result.reason = "locked_roi_below_threshold";
        }
        return result;
    }

    if (unmatched_down > 0) {
        const double quantity = std::min({unmatched_down, row.target_size, row.up_depth});
        const double average_down_cost = row.inventory.down_cost /
            std::max(row.inventory.down_quantity, 1e-12);
        result.outcome = "Up";
        result.quantity = quantity;
        result.unit_cost = row.up_unit_cost;
        result.projected_locked_quantity = quantity;
        result.projected_locked_profit = quantity * (1 - average_down_cost - row.up_unit_cost);
        const double pair_cost = quantity * (average_down_cost + row.up_unit_cost);
        result.projected_locked_roi = pair_cost > 0
            ? result.projected_locked_profit / pair_cost : 0;
        const double held_cost = quantity * average_down_cost;
        result.guaranteed_loss = std::max(0.0, -result.projected_locked_profit);
        result.loss_reduction_ratio = held_cost > 0
            ? 1 - result.guaranteed_loss / held_cost : 0;
        result.projected_residual_quantity = unmatched_down - quantity;
        if (quantity <= 0) result.reason = "up_depth";
        else if (
            result.projected_locked_profit >= row.minimum_locked_profit
            && result.projected_locked_roi >= row.minimum_locked_roi
        ) {
            result.decision = "ACCEPT";
            result.reason = "inventory_lock";
            result.action = "BUY_UP_AND_LOCK";
        } else if (
            row.allow_loss_cap
            && result.guaranteed_loss <= row.maximum_loss_cap
            && result.loss_reduction_ratio >= row.minimum_loss_reduction_ratio
        ) {
            result.decision = "ACCEPT";
            result.reason = "legacy_inventory_loss_cap";
            result.action = "BUY_UP_AND_CAP_LOSS";
        } else if (result.projected_locked_profit < row.minimum_locked_profit) {
            result.reason = "complement_cost_above_lock_threshold";
        } else {
            result.reason = "locked_roi_below_threshold";
        }
        return result;
    }

    const double up_edge = row.up_probability - row.up_unit_cost;
    const double down_probability = 1 - row.up_probability;
    const double down_edge = down_probability - row.down_unit_cost;
    const bool buy_up = up_edge >= down_edge;
    result.outcome = buy_up ? "Up" : "Down";
    result.probability = buy_up ? row.up_probability : down_probability;
    result.unit_cost = buy_up ? row.up_unit_cost : row.down_unit_cost;
    result.probability_edge = buy_up ? up_edge : down_edge;
    result.expected_value_roi = result.unit_cost > 0
        ? result.probability_edge / result.unit_cost : 0;
    const double depth = buy_up ? row.up_depth : row.down_depth;
    const double complement_cost = buy_up ? row.down_unit_cost : row.up_unit_cost;
    const double maximum_pair_cost = 1 / (1 + row.minimum_locked_roi);
    const double maximum_complement_cost = maximum_pair_cost - result.unit_cost;
    result.complement_gap = std::max(0.0, complement_cost - maximum_complement_cost);
    const double notional_quantity = result.unit_cost > 0
        ? row.maximum_unmatched_notional / result.unit_cost : 0;
    result.quantity = std::min({row.target_size, depth, notional_quantity});
    result.projected_residual_quantity = result.quantity;
    result.expected_value = result.quantity * result.probability_edge;
    result.maximum_loss = result.quantity * result.unit_cost;
    if (result.unit_cost > row.maximum_initial_price)
        result.reason = "initial_price_above_limit";
    else if (result.probability_edge < row.minimum_entry_edge)
        result.reason = "probability_edge_below_threshold";
    else if (result.expected_value_roi < row.minimum_entry_ev_roi)
        result.reason = "entry_ev_roi_below_threshold";
    else if (result.complement_gap > row.maximum_complement_gap)
        result.reason = "complement_gap_above_limit";
    else if (row.maximum_unmatched_notional <= 0)
        result.reason = "unmatched_notional_limit";
    else if (result.quantity <= 0)
        result.reason = buy_up ? "up_depth" : "down_depth";
    else {
        result.decision = "ACCEPT";
        result.reason = "inventory_accumulation";
        result.action = buy_up ? "BUY_UP" : "BUY_DOWN";
    }
    return result;
}

struct MakerInput {
    double up_probability = 0;
    double up_best_bid = 0;
    double up_best_ask = 1;
    double down_best_bid = 0;
    double down_best_ask = 1;
    double tick_size = .01;
    double quote_half_spread = .02;
    double inventory_skew = 0;
    double expected_rebate_per_pair = 0;
    double minimum_pair_edge = .01;
    double both_fill_probability = 0;
    double orphan_loss = 0;
};

struct SplitSellInput {
    double target_size = 0;
    double up_fill = 0;
    double down_fill = 0;
    double up_vwap = 0;
    double down_vwap = 0;
    double up_fee = 0;
    double down_fee = 0;
    double execution_buffer = 0;
    double minimum_locked_profit = .01;
    double minimum_expected_value = .01;
    double leg_1_fill_probability = 0;
    double leg_2_fill_probability = 0;
    double orphan_leg_loss = 0;
};

struct SplitSellDecision {
    std::string decision = "REJECT";
    std::string reason = "sell_depth";
    double gross_proceeds = 0;
    double total_fees = 0;
    double net_proceeds = 0;
    double collateral_cost = 0;
    double locked_profit = 0;
    double locked_roi = 0;
    double expected_execution_value = 0;
};

inline SplitSellDecision evaluate_split_sell(const SplitSellInput& row) {
    SplitSellDecision result;
    result.gross_proceeds = row.target_size * (row.up_vwap + row.down_vwap);
    result.total_fees = row.up_fee + row.down_fee;
    result.net_proceeds = result.gross_proceeds - result.total_fees -
        row.execution_buffer;
    result.collateral_cost = row.target_size;
    result.locked_profit = result.net_proceeds - result.collateral_cost;
    result.locked_roi = result.collateral_cost > 0
        ? result.locked_profit / result.collateral_cost : 0;
    const double both_fill_probability = row.leg_1_fill_probability *
        row.leg_2_fill_probability;
    result.expected_execution_value = both_fill_probability * result.locked_profit -
        row.leg_1_fill_probability * (1 - row.leg_2_fill_probability) *
        row.orphan_leg_loss;
    if (row.up_fill < row.target_size) result.reason = "up_bid_depth";
    else if (row.down_fill < row.target_size) result.reason = "down_bid_depth";
    else if (result.locked_profit < row.minimum_locked_profit)
        result.reason = "split_sell_profit_below_threshold";
    else if (result.expected_execution_value < row.minimum_expected_value)
        result.reason = "split_sell_execution_value_below_threshold";
    else {
        result.decision = "ACCEPT";
        result.reason = "split_sell_opportunity";
    }
    return result;
}

struct MakerDecision {
    std::string decision = "REJECT";
    std::string reason = "maker_pair_edge_below_threshold";
    bool quote_geometry_qualified = false;
    double up_bid = 0;
    double down_bid = 0;
    double pair_cost = 0;
    double locked_edge = 0;
    double expected_value = 0;
};

inline double floor_tick(double value, double tick) {
    return tick > 0 ? std::floor(value / tick + 1e-12) * tick : value;
}

inline MakerDecision evaluate_maker(const MakerInput& row) {
    MakerDecision result;
    const double up_fair = std::clamp(row.up_probability, .001, .999);
    const double down_fair = 1 - up_fair;
    result.up_bid = floor_tick(
        up_fair - row.quote_half_spread - row.inventory_skew, row.tick_size);
    result.down_bid = floor_tick(
        down_fair - row.quote_half_spread + row.inventory_skew, row.tick_size);
    result.up_bid = std::clamp(result.up_bid, row.tick_size, 1 - row.tick_size);
    result.down_bid = std::clamp(result.down_bid, row.tick_size, 1 - row.tick_size);
    if (result.up_bid >= row.up_best_ask || result.down_bid >= row.down_best_ask) {
        result.reason = "post_only_would_cross";
        return result;
    }
    result.pair_cost = result.up_bid + result.down_bid;
    result.locked_edge = 1 - result.pair_cost;
    result.expected_value = row.both_fill_probability *
        (result.locked_edge + row.expected_rebate_per_pair) -
        (1 - row.both_fill_probability) * row.orphan_loss;
    if (result.locked_edge < row.minimum_pair_edge)
        return result;
    result.quote_geometry_qualified = true;
    if (row.both_fill_probability <= 0) {
        result.reason = "maker_fill_probability_unavailable";
        return result;
    }
    if (result.expected_value <= 0) {
        result.reason = "maker_expected_value_below_threshold";
        return result;
    }
    result.decision = "ACCEPT";
    result.reason = "maker_quote_candidate";
    return result;
}

}
