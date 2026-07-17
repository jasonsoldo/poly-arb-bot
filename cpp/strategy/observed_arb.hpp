#pragma once

#include <algorithm>
#include <string>

namespace observed_arb {

enum class LegOrder {
    UP_THEN_DOWN,
    DOWN_THEN_UP,
};

enum class State {
    PENDING,
    BOOK_EXECUTABLE,
    ORPHANED,
    INVALIDATED,
};

struct AttemptIdentity {
    std::string attempt_id;
    std::string market_id;
    std::string condition_id;
    unsigned long long generation = 0;
    unsigned long long session = 0;
};

struct BookLeg {
    double requested_quantity = 0;
    double executable_quantity = 0;
    double vwap = 0;
    double gross_value = 0;
    double rounded_fee = 0;
    double age_ms = 0;
    bool snapshot = false;
    bool fresh = false;
    bool synced = false;
    bool crossed = false;
    unsigned long long generation = 0;
    unsigned long long session = 0;
};

struct Attempt {
    AttemptIdentity identity;
    LegOrder order = LegOrder::UP_THEN_DOWN;
    double target_size = 0;
    double guaranteed_payout = 0;
    double execution_buffer = 0;
    BookLeg first_leg;
    double started_us = 0;
    double due_us = 0;
    bool valid = false;
    std::string reason;
};

struct Outcome {
    State state = State::INVALIDATED;
    LegOrder order = LegOrder::UP_THEN_DOWN;
    std::string reason;
    double net_cost = 0;
    double locked_profit = 0;
    double orphan_pnl = 0;
    bool first_leg_book_executable = false;
    bool both_legs_book_executable = false;
};

inline std::string invalid_reason(
    const BookLeg& leg,
    const AttemptIdentity& identity,
    const std::string& prefix
) {
    if (leg.generation != identity.generation) return "generation_changed";
    if (leg.session != identity.session) return "session_changed";
    if (!leg.snapshot) return prefix + "_missing_snapshot";
    if (!leg.fresh) return prefix + "_stale";
    if (!leg.synced) return prefix + "_not_synced";
    if (leg.crossed) return prefix + "_crossed";
    if (leg.requested_quantity <= 0 || leg.executable_quantity + 1e-12 < leg.requested_quantity)
        return prefix + "_depth";
    if (leg.vwap <= 0 || leg.gross_value <= 0) return prefix + "_price";
    if (leg.rounded_fee < 0) return prefix + "_fee";
    return {};
}

inline Attempt start_buy_both(
    const AttemptIdentity& identity,
    LegOrder order,
    double target_size,
    double guaranteed_payout,
    double execution_buffer,
    BookLeg first_leg,
    double started_us,
    double due_us
) {
    Attempt result;
    result.identity = identity;
    result.order = order;
    result.target_size = target_size;
    result.guaranteed_payout = guaranteed_payout;
    result.execution_buffer = execution_buffer;
    result.first_leg = first_leg;
    result.started_us = started_us;
    result.due_us = due_us;
    if (target_size <= 0 || guaranteed_payout <= 0 || execution_buffer < 0) {
        result.reason = "invalid_attempt_parameters";
        return result;
    }
    first_leg.requested_quantity = target_size;
    result.first_leg.requested_quantity = target_size;
    result.reason = invalid_reason(result.first_leg, identity, "first_leg");
    result.valid = result.reason.empty();
    if (result.valid) result.reason = "started";
    return result;
}

inline double conservative_orphan_pnl(
    const Attempt& attempt,
    const BookLeg& first_exit
) {
    const double first_cost = attempt.first_leg.gross_value + attempt.first_leg.rounded_fee;
    const std::string exit_error = invalid_reason(first_exit, attempt.identity, "first_exit");
    if (!exit_error.empty()) return -first_cost - attempt.execution_buffer;
    return first_exit.gross_value - first_exit.rounded_fee - first_cost -
        attempt.execution_buffer;
}

inline Outcome observe_buy_both(
    const Attempt& attempt,
    BookLeg second_leg,
    BookLeg first_exit,
    double observed_us
) {
    Outcome result;
    result.order = attempt.order;
    result.first_leg_book_executable = attempt.valid;
    if (!attempt.valid) {
        result.reason = attempt.reason;
        return result;
    }
    if (observed_us < attempt.due_us) {
        result.state = State::PENDING;
        result.reason = "delay_pending";
        return result;
    }
    second_leg.requested_quantity = attempt.target_size;
    first_exit.requested_quantity = attempt.target_size;
    const std::string second_error = invalid_reason(second_leg, attempt.identity, "second_leg");
    if (!second_error.empty()) {
        result.state = (
            second_error == "generation_changed" || second_error == "session_changed" ||
            second_error == "second_leg_missing_snapshot" || second_error == "second_leg_stale" ||
            second_error == "second_leg_not_synced" || second_error == "second_leg_crossed"
        ) ? State::INVALIDATED : State::ORPHANED;
        result.reason = second_error;
        result.orphan_pnl = conservative_orphan_pnl(attempt, first_exit);
        return result;
    }
    result.net_cost = attempt.first_leg.gross_value + attempt.first_leg.rounded_fee +
        second_leg.gross_value + second_leg.rounded_fee + attempt.execution_buffer;
    result.locked_profit = attempt.guaranteed_payout - result.net_cost;
    if (result.locked_profit <= 0) {
        result.state = State::ORPHANED;
        result.reason = "delayed_profit_non_positive";
        result.orphan_pnl = conservative_orphan_pnl(attempt, first_exit);
        return result;
    }
    result.state = State::BOOK_EXECUTABLE;
    result.reason = "book_executable";
    result.both_legs_book_executable = true;
    return result;
}

}  // namespace observed_arb
