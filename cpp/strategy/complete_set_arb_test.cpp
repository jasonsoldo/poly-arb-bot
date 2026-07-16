#include "complete_set_arb.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    {
        complete_set::SplitSellInput row;
        row.target_size = 10;
        row.up_fill = row.down_fill = 10;
        row.up_vwap = .54;
        row.down_vwap = .49;
        row.up_fee = .03;
        row.down_fee = .03;
        row.execution_buffer = .02;
        row.leg_1_fill_probability = 1;
        row.leg_2_fill_probability = .95;
        row.orphan_leg_loss = .10;
        const auto result = complete_set::evaluate_split_sell(row);
        assert(result.decision == "ACCEPT");
        assert(result.reason == "split_sell_opportunity");
        assert(std::abs(result.gross_proceeds - 10.3) < 1e-12);
        assert(std::abs(result.locked_profit - .22) < 1e-12);
        assert(result.expected_execution_value > 0);
    }
    {
        complete_set::SplitSellInput row;
        row.target_size = 10;
        row.up_fill = row.down_fill = 10;
        row.up_vwap = .51;
        row.down_vwap = .49;
        row.execution_buffer = .02;
        row.leg_1_fill_probability = row.leg_2_fill_probability = 1;
        const auto result = complete_set::evaluate_split_sell(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "split_sell_profit_below_threshold");
    }
    {
        complete_set::SplitSellInput row;
        row.target_size = 10;
        row.up_fill = 9;
        row.down_fill = 10;
        const auto result = complete_set::evaluate_split_sell(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "up_bid_depth");
    }
    {
        complete_set::RebalanceInput row;
        row.inventory = {10, 0, 4.2, 0};
        row.target_size = 10;
        row.up_probability = .6;
        row.up_unit_cost = .5;
        row.down_unit_cost = .52;
        row.up_depth = row.down_depth = 20;
        row.minimum_locked_roi = .02;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "ACCEPT");
        assert(result.action == "BUY_DOWN_AND_LOCK");
        assert(std::abs(result.projected_locked_profit - .6) < 1e-12);
        assert(result.projected_locked_roi > .02);
    }
    {
        complete_set::RebalanceInput row;
        row.target_size = 10;
        row.up_probability = .30;
        row.up_unit_cost = .20;
        row.down_unit_cost = .81;
        row.up_depth = row.down_depth = 20;
        row.maximum_unmatched_notional = .50;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "ACCEPT");
        assert(result.action == "BUY_UP");
        assert(std::abs(result.probability_edge - .10) < 1e-12);
        assert(std::abs(result.maximum_loss - .50) < 1e-12);
        assert(result.expected_value_roi > .25);
    }
    {
        complete_set::RebalanceInput row;
        row.target_size = 10;
        row.up_probability = .8;
        row.up_unit_cost = .65;
        row.down_unit_cost = .4;
        row.up_depth = row.down_depth = 20;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "initial_price_above_limit");
    }
    {
        complete_set::RebalanceInput row;
        row.target_size = 10;
        row.up_probability = .26;
        row.up_unit_cost = .20;
        row.down_unit_cost = .86;
        row.up_depth = row.down_depth = 20;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "complement_gap_above_limit");
    }
    {
        complete_set::RebalanceInput row;
        row.target_size = 10;
        row.up_probability = .30;
        row.up_unit_cost = .20;
        row.down_unit_cost = .81;
        row.up_depth = row.down_depth = 20;
        row.maximum_unmatched_notional = 0;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "unmatched_notional_limit");
    }
    {
        complete_set::RebalanceInput row;
        row.inventory = {10, 0, 4.8, 0};
        row.target_size = 10;
        row.up_probability = .6;
        row.up_unit_cost = .5;
        row.down_unit_cost = .51;
        row.up_depth = row.down_depth = 20;
        row.minimum_locked_profit = .01;
        row.minimum_locked_roi = .02;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "locked_roi_below_threshold");
    }
    {
        complete_set::RebalanceInput row;
        row.inventory = {10, 0, 8.6, 0};
        row.target_size = 10;
        row.up_probability = .9;
        row.up_unit_cost = .87;
        row.down_unit_cost = .18;
        row.up_depth = row.down_depth = 20;
        row.allow_loss_cap = true;
        row.maximum_loss_cap = .50;
        row.minimum_loss_reduction_ratio = .75;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "ACCEPT");
        assert(result.action == "BUY_DOWN_AND_CAP_LOSS");
        assert(result.reason == "legacy_inventory_loss_cap");
        assert(std::abs(result.guaranteed_loss - .4) < 1e-12);
        assert(result.loss_reduction_ratio > .9);
    }
    {
        complete_set::RebalanceInput row;
        row.inventory = {10, 0, 8.6, 0};
        row.target_size = 10;
        row.up_probability = .9;
        row.up_unit_cost = .87;
        row.down_unit_cost = .30;
        row.up_depth = row.down_depth = 20;
        row.allow_loss_cap = true;
        row.maximum_loss_cap = .50;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "complement_cost_above_lock_threshold");
    }
    {
        complete_set::MakerInput row;
        row.up_probability = .55;
        row.up_best_ask = .56;
        row.down_best_ask = .46;
        row.both_fill_probability = .9;
        row.orphan_loss = .01;
        const auto result = complete_set::evaluate_maker(row);
        assert(result.decision == "ACCEPT");
        assert(result.up_bid + result.down_bid < 1);
        assert(result.expected_value > 0);
    }
    {
        complete_set::MakerInput row;
        row.up_probability = .55;
        row.up_best_ask = .50;
        row.down_best_ask = .46;
        const auto result = complete_set::evaluate_maker(row);
        assert(result.decision == "REJECT");
        assert(result.reason == "post_only_would_cross");
    }
    {
        complete_set::MakerInput row;
        row.up_probability = .55;
        row.up_best_ask = .56;
        row.down_best_ask = .46;
        const auto result = complete_set::evaluate_maker(row);
        assert(result.decision == "REJECT");
        assert(result.quote_geometry_qualified);
        assert(result.reason == "maker_fill_probability_unavailable");
        assert(result.locked_edge >= row.minimum_pair_edge);
    }
    std::cout << "complete-set arbitrage tests passed\n";
}
