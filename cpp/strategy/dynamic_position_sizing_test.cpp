#include "dynamic_position_sizing.hpp"

#include <cassert>
#include <cmath>
#include <iostream>
#include <map>

namespace {

using Book = std::map<double, double>;

sizing::ProbabilityConfig probability_config() {
    sizing::ProbabilityConfig config;
    config.shadow_capital_usd = 1000;
    config.fractional_kelly = 0.10;
    config.maximum_capital_fraction = 0.05;
    config.probability_haircut = 0.02;
    config.maximum_quantity = 100;
    config.minimum_order_size = 1;
    config.maximum_slippage_per_share = 0.02;
    config.minimum_net_ev_per_share = 0.01;
    config.fee_rate = 0.07;
    config.execution_buffer_per_share = 0.005;
    return config;
}

void test_probability_size_uses_real_depth() {
    const Book deep{{0.40, 100}};
    const Book shallow{{0.40, 2}};
    const auto config = probability_config();

    const auto deep_result = sizing::size_probability_position(deep, 0.70, 0.90, config);
    const auto shallow_result = sizing::size_probability_position(shallow, 0.70, 0.90, config);

    assert(deep_result.accepted);
    assert(shallow_result.accepted);
    assert(deep_result.dynamic_target_size > shallow_result.dynamic_target_size);
    assert(shallow_result.dynamic_target_size <= 2 + 1e-9);
    assert(shallow_result.size_binding_constraint == "executable_depth");
}

void test_probability_size_shrinks_with_quality_and_haircut() {
    const Book asks{{0.40, 100}};
    auto config = probability_config();
    const auto high_quality = sizing::size_probability_position(asks, 0.70, 1.0, config);
    const auto low_quality = sizing::size_probability_position(asks, 0.70, 0.4, config);
    config.probability_haircut = 0.08;
    const auto larger_haircut = sizing::size_probability_position(asks, 0.70, 1.0, config);

    assert(high_quality.accepted);
    assert(low_quality.accepted);
    assert(high_quality.dynamic_target_size > low_quality.dynamic_target_size);
    assert(high_quality.dynamic_target_size > larger_haircut.dynamic_target_size);
    assert(high_quality.conservative_probability < high_quality.estimated_probability);
}

void test_probability_size_accounts_for_vwap_fee_and_slippage() {
    const Book asks{{0.40, 2}, {0.50, 100}};
    auto config = probability_config();
    config.maximum_slippage_per_share = 0.01;
    const auto result = sizing::size_probability_position(asks, 0.70, 1.0, config);

    assert(result.accepted);
    assert(result.dynamic_target_size <= 2.23);
    assert(result.size_binding_constraint == "slippage_limit");
    assert(result.dynamic_fee > 0);
    assert(result.dynamic_all_in_cost > result.dynamic_vwap * result.dynamic_target_size);
    assert(result.dynamic_all_in_price > result.dynamic_vwap);
    assert(result.dynamic_expected_profit > 0);
    assert(result.dynamic_maximum_loss == result.dynamic_all_in_cost);
}

void test_probability_size_fails_closed() {
    auto config = probability_config();
    config.shadow_capital_usd = 0;
    assert(sizing::size_probability_position({{0.40, 100}}, 0.70, 1.0, config).reason ==
           "sizing_capital_unavailable");

    config = probability_config();
    assert(sizing::size_probability_position({}, 0.70, 1.0, config).reason ==
           "dynamic_depth_unavailable");
    assert(sizing::size_probability_position({{0.40, 0.5}}, 0.70, 1.0, config).reason ==
           "dynamic_size_below_market_minimum");
    assert(sizing::size_probability_position({{0.40, 100}}, std::nan(""), 1.0, config).reason ==
           "sizing_probability_unavailable");
}

sizing::PairedConfig paired_config() {
    sizing::PairedConfig config;
    config.shadow_capital_usd = 1000;
    config.maximum_capital_fraction = 0.02;
    config.maximum_quantity = 100;
    config.minimum_order_size = 1;
    config.maximum_slippage_per_share = 0.02;
    config.fee_rate = 0.07;
    config.execution_buffer_per_share = 0.002;
    config.minimum_locked_profit = 0.01;
    config.minimum_locked_roi = 0.001;
    return config;
}

void test_paired_size_uses_equal_real_depth_and_cost_chain() {
    const Book up{{0.40, 50}};
    const Book down{{0.50, 3}};
    const auto result = sizing::size_paired_lock(up, down, paired_config());

    assert(result.accepted);
    assert(result.dynamic_target_size <= 3 + 1e-9);
    assert(std::abs(result.guaranteed_payout - result.dynamic_target_size) < 1e-9);
    assert(result.dynamic_expected_profit == result.locked_profit);
    assert(result.locked_profit > 0);
    assert(std::abs(result.up_vwap - 0.40) < 1e-12);
    assert(std::abs(result.down_vwap - 0.50) < 1e-12);
    assert(result.up_fee > 0);
    assert(result.down_fee > 0);
    assert(result.size_binding_constraint == "executable_depth");
}

void test_paired_size_rejects_cost_above_payout() {
    const auto result = sizing::size_paired_lock(
        {{0.55, 100}}, {{0.55, 100}}, paired_config());
    assert(!result.accepted);
    assert(result.reason == "net_cost_above_threshold");
}

void test_paired_size_can_cross_absolute_profit_at_larger_real_size() {
    auto config = paired_config();
    config.minimum_locked_profit = 0.05;
    const auto result = sizing::size_paired_lock(
        {{0.45, 100}}, {{0.50, 100}}, config);

    assert(result.accepted);
    assert(result.dynamic_target_size > config.minimum_order_size);
    assert(result.locked_profit >= config.minimum_locked_profit);
}

}  // namespace

int main() {
    test_probability_size_uses_real_depth();
    test_probability_size_shrinks_with_quality_and_haircut();
    test_probability_size_accounts_for_vwap_fee_and_slippage();
    test_probability_size_fails_closed();
    test_paired_size_uses_equal_real_depth_and_cost_chain();
    test_paired_size_rejects_cost_above_payout();
    test_paired_size_can_cross_absolute_profit_at_larger_real_size();
    std::cout << "dynamic position sizing tests passed\n";
}
