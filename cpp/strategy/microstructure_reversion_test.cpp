#include "microstructure_reversion.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

namespace {

bool close(double left, double right) {
    return std::abs(left - right) < 1e-9;
}

microstructure_reversion::BookFill fill(
    double executable_quantity,
    double vwap,
    double fee,
    unsigned long long generation = 2,
    unsigned long long session = 3
) {
    microstructure_reversion::BookFill result;
    result.requested_quantity = 10;
    result.executable_quantity = executable_quantity;
    result.vwap = vwap;
    result.gross_value = executable_quantity * vwap;
    result.rounded_fee = fee;
    result.age_ms = 4;
    result.snapshot = true;
    result.fresh = true;
    result.crossed = false;
    result.generation = generation;
    result.session = session;
    return result;
}

microstructure_reversion::Identity identity() {
    return {"attempt-1", "market-1", "condition-1", "token-up", 2, 3};
}

microstructure_reversion::EntryInput valid_entry() {
    microstructure_reversion::EntryInput row;
    row.identity = identity();
    row.outcome = "Up";
    row.target_size = 10;
    row.robust_anchor = .23;
    row.sample_count = 30;
    row.sample_span_ms = 4500;
    row.minimum_samples = 20;
    row.minimum_sample_span_ms = 2000;
    row.minimum_discount_per_share = .02;
    row.maximum_spread = .05;
    row.spread = .02;
    row.seconds_to_close = 60;
    row.maximum_holding_ms = 5000;
    row.minimum_exit_margin_seconds = 10;
    row.entry_execution_buffer = .005;
    row.minimum_profit = .01;
    row.buy = fill(10, .20, .01);
    row.observed_us = 1000000;
    return row;
}

void test_real_ask_discount_starts_book_executable_shadow_entry() {
    const auto result = microstructure_reversion::evaluate_entry(valid_entry());
    assert(result.state == microstructure_reversion::State::ENTRY_BOOK_EXECUTABLE);
    assert(result.reason == "discount_entry_book_executable");
    assert(close(result.discount_per_share, .03));
    assert(close(result.position.entry_total_cost, 2.015));
    assert(result.position.identity.token_id == "token-up");
}

void test_insufficient_real_history_rejects_underpricing_claim() {
    auto row = valid_entry();
    row.sample_count = 4;
    const auto result = microstructure_reversion::evaluate_entry(row);
    assert(result.state == microstructure_reversion::State::REJECTED);
    assert(result.reason == "insufficient_midpoint_samples");
}

void test_future_real_bid_depth_can_lock_net_profit() {
    const auto entry = microstructure_reversion::evaluate_entry(valid_entry());
    microstructure_reversion::ExitInput row;
    row.position = entry.position;
    row.sell = fill(10, .23, .01);
    row.exit_execution_buffer = .005;
    row.observed_us = 1250000;
    const auto result = microstructure_reversion::evaluate_exit(row);
    assert(result.state == microstructure_reversion::State::PROFIT_EXIT_BOOK_EXECUTABLE);
    assert(result.reason == "net_profit_exit_book_executable");
    assert(close(result.net_exit_value, 2.285));
    assert(close(result.net_profit, .27));
}

void test_visible_price_without_full_bid_depth_is_not_a_shadow_fill() {
    const auto entry = microstructure_reversion::evaluate_entry(valid_entry());
    microstructure_reversion::ExitInput row;
    row.position = entry.position;
    row.sell = fill(5, .30, .005);
    row.exit_execution_buffer = .005;
    row.observed_us = 1250000;
    const auto result = microstructure_reversion::evaluate_exit(row);
    assert(result.state == microstructure_reversion::State::HOLDING);
    assert(result.reason == "exit_depth");
    assert(!result.exit_book_executable);
}

void test_timeout_uses_observed_bid_and_records_real_shadow_loss() {
    const auto entry = microstructure_reversion::evaluate_entry(valid_entry());
    microstructure_reversion::ExitInput row;
    row.position = entry.position;
    row.sell = fill(10, .18, .01);
    row.exit_execution_buffer = .005;
    row.observed_us = entry.position.opened_us + 5000000;
    const auto result = microstructure_reversion::evaluate_exit(row);
    assert(result.state == microstructure_reversion::State::TIMEOUT_EXIT_BOOK_EXECUTABLE);
    assert(result.reason == "maximum_holding_time_exit_book_executable");
    assert(close(result.net_profit, -.23));
}

void test_timeout_without_full_depth_is_no_exit_not_fake_pnl() {
    const auto entry = microstructure_reversion::evaluate_entry(valid_entry());
    microstructure_reversion::ExitInput row;
    row.position = entry.position;
    row.sell = fill(3, .18, .003);
    row.exit_execution_buffer = .005;
    row.observed_us = entry.position.opened_us + 5000000;
    const auto result = microstructure_reversion::evaluate_exit(row);
    assert(result.state == microstructure_reversion::State::NO_EXIT);
    assert(result.reason == "maximum_holding_time_exit_depth");
    assert(!result.exit_book_executable);
}

void test_new_websocket_session_invalidates_old_shadow_position() {
    const auto entry = microstructure_reversion::evaluate_entry(valid_entry());
    microstructure_reversion::ExitInput row;
    row.position = entry.position;
    row.sell = fill(10, .23, .01, 2, 4);
    row.observed_us = 1250000;
    const auto result = microstructure_reversion::evaluate_exit(row);
    assert(result.state == microstructure_reversion::State::INVALIDATED);
    assert(result.reason == "session_changed");
}

}  // namespace

int main() {
    test_real_ask_discount_starts_book_executable_shadow_entry();
    test_insufficient_real_history_rejects_underpricing_claim();
    test_future_real_bid_depth_can_lock_net_profit();
    test_visible_price_without_full_bid_depth_is_not_a_shadow_fill();
    test_timeout_uses_observed_bid_and_records_real_shadow_loss();
    test_timeout_without_full_depth_is_no_exit_not_fake_pnl();
    test_new_websocket_session_invalidates_old_shadow_position();
    std::cout << "microstructure reversion tests passed\n";
}
