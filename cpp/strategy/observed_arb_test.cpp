#include "observed_arb.hpp"

#include <cassert>
#include <cmath>
#include <iostream>

namespace {

bool close(double left, double right) {
    return std::abs(left - right) < 1e-9;
}

observed_arb::BookLeg leg(
    double quantity, double vwap, double fee,
    unsigned long long generation = 2,
    unsigned long long session = 3
) {
    observed_arb::BookLeg result;
    result.requested_quantity = 10;
    result.executable_quantity = quantity;
    result.vwap = vwap;
    result.gross_value = quantity * vwap;
    result.rounded_fee = fee;
    result.age_ms = 10;
    result.snapshot = true;
    result.fresh = true;
    result.synced = true;
    result.crossed = false;
    result.generation = generation;
    result.session = session;
    return result;
}

observed_arb::AttemptIdentity identity() {
    return {"attempt-1", "market-1", "condition-1", 2, 3};
}

void test_book_executable_uses_delayed_second_leg() {
    const auto attempt = observed_arb::start_buy_both(
        identity(), observed_arb::LegOrder::UP_THEN_DOWN,
        10, 10, 0.02, leg(10, 0.47, 0.01), 1000, 51000
    );
    assert(attempt.valid);

    const auto result = observed_arb::observe_buy_both(
        attempt, leg(10, 0.50, 0.02), leg(10, 0.46, 0.01), 51000
    );
    assert(result.state == observed_arb::State::BOOK_EXECUTABLE);
    assert(result.reason == "book_executable");
    assert(close(result.net_cost, 9.75));
    assert(close(result.locked_profit, 0.25));
    assert(result.first_leg_book_executable);
    assert(result.both_legs_book_executable);
}

void test_leg_order_is_preserved() {
    const auto attempt = observed_arb::start_buy_both(
        identity(), observed_arb::LegOrder::DOWN_THEN_UP,
        10, 10, 0.02, leg(10, 0.49, 0.01), 0, 50000
    );
    const auto result = observed_arb::observe_buy_both(
        attempt, leg(10, 0.48, 0.01), leg(10, 0.47, 0.01), 50000
    );
    assert(result.order == observed_arb::LegOrder::DOWN_THEN_UP);
    assert(close(result.locked_profit, 0.26));
}

void test_missing_second_leg_depth_is_orphaned_with_exit_pnl() {
    const auto attempt = observed_arb::start_buy_both(
        identity(), observed_arb::LegOrder::UP_THEN_DOWN,
        10, 10, 0.02, leg(10, 0.48, 0.01), 0, 50000
    );
    const auto result = observed_arb::observe_buy_both(
        attempt, leg(7, 0.49, 0.01), leg(10, 0.46, 0.01), 50000
    );
    assert(result.state == observed_arb::State::ORPHANED);
    assert(result.reason == "second_leg_depth");
    assert(close(result.orphan_pnl, -0.24));
    assert(!result.both_legs_book_executable);
}

void test_missing_exit_depth_uses_full_first_leg_loss() {
    const auto attempt = observed_arb::start_buy_both(
        identity(), observed_arb::LegOrder::UP_THEN_DOWN,
        10, 10, 0.02, leg(10, 0.48, 0.01), 0, 50000
    );
    const auto result = observed_arb::observe_buy_both(
        attempt, leg(0, 0, 0), leg(5, 0.46, 0.01), 50000
    );
    assert(result.state == observed_arb::State::ORPHANED);
    assert(close(result.orphan_pnl, -4.83));
}

void test_session_change_invalidates_without_reusing_book() {
    const auto attempt = observed_arb::start_buy_both(
        identity(), observed_arb::LegOrder::UP_THEN_DOWN,
        10, 10, 0.02, leg(10, 0.48, 0.01), 0, 50000
    );
    const auto result = observed_arb::observe_buy_both(
        attempt, leg(10, 0.49, 0.01, 2, 4), leg(10, 0.46, 0.01, 2, 4), 50000
    );
    assert(result.state == observed_arb::State::INVALIDATED);
    assert(result.reason == "session_changed");
    assert(close(result.orphan_pnl, -4.83));
}

void test_unready_first_leg_does_not_start() {
    auto first = leg(10, 0.48, 0.01);
    first.snapshot = false;
    const auto attempt = observed_arb::start_buy_both(
        identity(), observed_arb::LegOrder::UP_THEN_DOWN,
        10, 10, 0.02, first, 0, 50000
    );
    assert(!attempt.valid);
    assert(attempt.reason == "first_leg_missing_snapshot");
}

}  // namespace

int main() {
    test_book_executable_uses_delayed_second_leg();
    test_leg_order_is_preserved();
    test_missing_second_leg_depth_is_orphaned_with_exit_pnl();
    test_missing_exit_depth_uses_full_first_leg_loss();
    test_session_change_invalidates_without_reusing_book();
    test_unready_first_leg_does_not_start();
    std::cout << "observed arbitrage tests passed\n";
}
