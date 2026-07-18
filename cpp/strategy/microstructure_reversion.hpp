#pragma once

#include <algorithm>
#include <string>

namespace microstructure_reversion {

enum class State {
    REJECTED,
    ENTRY_BOOK_EXECUTABLE,
    HOLDING,
    PROFIT_EXIT_BOOK_EXECUTABLE,
    TIMEOUT_EXIT_BOOK_EXECUTABLE,
    NO_EXIT,
    INVALIDATED,
};

struct Identity {
    std::string attempt_id;
    std::string market_id;
    std::string condition_id;
    std::string token_id;
    unsigned long long generation = 0;
    unsigned long long session = 0;
};

struct BookFill {
    double requested_quantity = 0;
    double executable_quantity = 0;
    double vwap = 0;
    double gross_value = 0;
    double rounded_fee = 0;
    double age_ms = 0;
    bool snapshot = false;
    bool fresh = false;
    bool crossed = false;
    unsigned long long generation = 0;
    unsigned long long session = 0;
};

struct Position {
    Identity identity;
    std::string outcome;
    double quantity = 0;
    double robust_anchor = 0;
    double entry_vwap = 0;
    double entry_gross_value = 0;
    double entry_fee = 0;
    double entry_execution_buffer = 0;
    double entry_total_cost = 0;
    double minimum_profit = 0;
    double opened_us = 0;
    double maximum_holding_ms = 0;
};

struct EntryInput {
    Identity identity;
    std::string outcome;
    double target_size = 0;
    double robust_anchor = 0;
    std::size_t sample_count = 0;
    double sample_span_ms = 0;
    std::size_t minimum_samples = 0;
    double minimum_sample_span_ms = 0;
    double minimum_discount_per_share = 0;
    double maximum_spread = 0;
    double spread = 0;
    double seconds_to_close = 0;
    double maximum_holding_ms = 0;
    double minimum_exit_margin_seconds = 0;
    double entry_execution_buffer = 0;
    double minimum_profit = 0;
    BookFill buy;
    double observed_us = 0;
};

struct EntryDecision {
    State state = State::REJECTED;
    std::string reason = "invalid_entry";
    double discount_per_share = 0;
    Position position;
};

struct ExitInput {
    Position position;
    BookFill sell;
    double exit_execution_buffer = 0;
    double observed_us = 0;
};

struct ExitDecision {
    State state = State::HOLDING;
    std::string reason = "holding";
    double gross_exit_value = 0;
    double exit_fee = 0;
    double net_exit_value = 0;
    double net_profit = 0;
    bool exit_book_executable = false;
};

inline std::string invalid_fill_reason(
    const BookFill& fill,
    const Identity& identity,
    const std::string& prefix
) {
    if (fill.generation != identity.generation) return "generation_changed";
    if (fill.session != identity.session) return "session_changed";
    if (!fill.snapshot) return prefix + "_missing_snapshot";
    if (!fill.fresh) return prefix + "_stale";
    if (fill.crossed) return prefix + "_crossed";
    if (fill.requested_quantity <= 0 ||
        fill.executable_quantity + 1e-12 < fill.requested_quantity)
        return prefix + "_depth";
    if (fill.vwap <= 0 || fill.gross_value <= 0) return prefix + "_price";
    if (fill.rounded_fee < 0) return prefix + "_fee";
    return {};
}

inline EntryDecision evaluate_entry(const EntryInput& row) {
    EntryDecision result;
    if (row.target_size <= 0 || row.robust_anchor <= 0 ||
        row.minimum_discount_per_share < 0 || row.maximum_holding_ms <= 0 ||
        row.entry_execution_buffer < 0 || row.minimum_profit < 0) {
        result.reason = "invalid_entry_parameters";
        return result;
    }
    if (row.sample_count < row.minimum_samples) {
        result.reason = "insufficient_midpoint_samples";
        return result;
    }
    if (row.sample_span_ms < row.minimum_sample_span_ms) {
        result.reason = "insufficient_midpoint_span";
        return result;
    }
    if (row.spread < 0 || row.spread > row.maximum_spread) {
        result.reason = "spread_above_limit";
        return result;
    }
    if (row.seconds_to_close <=
        row.maximum_holding_ms / 1000 + row.minimum_exit_margin_seconds) {
        result.reason = "insufficient_exit_time";
        return result;
    }
    EntryInput checked = row;
    checked.buy.requested_quantity = row.target_size;
    const std::string fill_error = invalid_fill_reason(
        checked.buy, row.identity, "entry");
    if (!fill_error.empty()) {
        result.reason = fill_error;
        return result;
    }
    result.discount_per_share = row.robust_anchor - checked.buy.vwap;
    if (result.discount_per_share + 1e-12 < row.minimum_discount_per_share) {
        result.reason = "discount_below_threshold";
        return result;
    }
    result.state = State::ENTRY_BOOK_EXECUTABLE;
    result.reason = "discount_entry_book_executable";
    result.position.identity = row.identity;
    result.position.outcome = row.outcome;
    result.position.quantity = row.target_size;
    result.position.robust_anchor = row.robust_anchor;
    result.position.entry_vwap = checked.buy.vwap;
    result.position.entry_gross_value = checked.buy.gross_value;
    result.position.entry_fee = checked.buy.rounded_fee;
    result.position.entry_execution_buffer = row.entry_execution_buffer;
    result.position.entry_total_cost = checked.buy.gross_value +
        checked.buy.rounded_fee + row.entry_execution_buffer;
    result.position.minimum_profit = row.minimum_profit;
    result.position.opened_us = row.observed_us;
    result.position.maximum_holding_ms = row.maximum_holding_ms;
    return result;
}

inline ExitDecision evaluate_exit(const ExitInput& row) {
    ExitDecision result;
    const double held_ms = std::max(
        0.0, (row.observed_us - row.position.opened_us) / 1000);
    const bool timed_out = held_ms >= row.position.maximum_holding_ms;
    BookFill checked = row.sell;
    checked.requested_quantity = row.position.quantity;
    const std::string fill_error = invalid_fill_reason(
        checked, row.position.identity, "exit");
    if (!fill_error.empty()) {
        if (fill_error == "generation_changed" || fill_error == "session_changed" ||
            fill_error == "exit_missing_snapshot" || fill_error == "exit_stale" ||
            fill_error == "exit_crossed") {
            result.state = State::INVALIDATED;
            result.reason = fill_error;
        } else if (timed_out) {
            result.state = State::NO_EXIT;
            result.reason = "maximum_holding_time_" + fill_error;
        } else {
            result.reason = fill_error;
        }
        return result;
    }
    result.exit_book_executable = true;
    result.gross_exit_value = checked.gross_value;
    result.exit_fee = checked.rounded_fee;
    result.net_exit_value = checked.gross_value - checked.rounded_fee -
        row.exit_execution_buffer;
    result.net_profit = result.net_exit_value - row.position.entry_total_cost;
    if (result.net_profit + 1e-12 >= row.position.minimum_profit) {
        result.state = State::PROFIT_EXIT_BOOK_EXECUTABLE;
        result.reason = "net_profit_exit_book_executable";
    } else if (timed_out) {
        result.state = State::TIMEOUT_EXIT_BOOK_EXECUTABLE;
        result.reason = "maximum_holding_time_exit_book_executable";
    }
    return result;
}

}  // namespace microstructure_reversion
