#include "complete_set_arb.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

int main() {
    {
        complete_set::RebalanceInput row;
        row.inventory = {10, 0, 4.2, 0};
        row.target_size = 10;
        row.up_probability = .6;
        row.up_unit_cost = .5;
        row.down_unit_cost = .52;
        row.up_depth = row.down_depth = 20;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "ACCEPT");
        assert(result.action == "BUY_DOWN_AND_LOCK");
        assert(std::abs(result.projected_locked_profit - .6) < 1e-12);
    }
    {
        complete_set::RebalanceInput row;
        row.target_size = 10;
        row.up_probability = .8;
        row.up_unit_cost = .65;
        row.down_unit_cost = .4;
        row.up_depth = row.down_depth = 20;
        const auto result = complete_set::evaluate_rebalance(row);
        assert(result.decision == "ACCEPT");
        assert(result.action == "BUY_UP");
        assert(std::abs(result.probability_edge - .15) < 1e-12);
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
    std::cout << "complete-set arbitrage tests passed\n";
}
