#include "ev_strategy.hpp"

#include <boost/property_tree/json_parser.hpp>
#include <boost/property_tree/ptree.hpp>

#include <iomanip>
#include <iostream>
#include <optional>
#include <sstream>
#include <string>

using boost::property_tree::ptree;

std::optional<double> optional_number(const ptree& row, const std::string& key, std::optional<double> fallback = std::nullopt) {
    const auto raw = row.get_optional<std::string>(key);
    if (!raw) return fallback;
    if (*raw == "null" || raw->empty()) return std::nullopt;
    return std::stod(*raw);
}

void write_optional(const std::optional<double>& value) {
    if (value) std::cout << *value;
    else std::cout << "null";
}

int main() {
    std::cout << std::setprecision(17);
    for (std::string line; std::getline(std::cin, line);) {
        if (line.empty()) continue;
        std::istringstream input(line);
        ptree row;
        boost::property_tree::read_json(input, row);
        const std::string mode = row.get<std::string>("mode");
        if (mode == "probability" || mode == "lottery_probability") {
            strategy::ProbabilityInput value;
            value.settlement_reference = optional_number(row, "settlement_reference");
            value.price_to_beat = optional_number(row, "price_to_beat");
            value.seconds_to_close = row.get<double>("seconds_to_close");
            value.volatility_per_sqrt_second = optional_number(row, "volatility_per_sqrt_second");
            value.model_sample_count = row.get<int>("model_sample_count");
            value.model_sample_span_seconds = row.get<double>("model_sample_span_seconds");
            value.momentum_bps_30s = optional_number(row, "momentum_bps_30s");
            value.paired_book_imbalance = optional_number(row, "paired_book_imbalance");
            const auto output = mode == "probability"
                ? strategy::probability_model(value)
                : strategy::lottery_probability_model(value);
            std::cout << '{';
            if (mode == "lottery_probability") {
                std::cout << "\"raw_estimated_probability\":";
                write_optional(output.estimated_probability);
                std::cout << ',';
            }
            std::cout << "\"estimated_probability\":";
            write_optional(mode == "lottery_probability"
                ? strategy::lottery_market_blend_probability(
                    output.estimated_probability, row.get<double>("market_implied_probability"))
                : output.estimated_probability);
            std::cout << "}\n";
            continue;
        }
        strategy::EvaluationInput value;
        value.strategy = row.get<std::string>("strategy");
        value.timeframe = row.get<std::string>("timeframe");
        value.expected_fill_price = row.get<double>("expected_fill_price");
        value.estimated_probability = optional_number(row, "estimated_probability");
        value.seconds_to_close = row.get<int>("seconds_to_close");
        value.price_to_beat = optional_number(row, "price_to_beat", 100);
        value.fee_per_share = row.get<double>("fee_per_share", .01);
        value.slippage_per_share = row.get<double>("slippage_per_share", .002);
        value.liquidity = row.get<double>("liquidity", 100);
        value.book_age_ms = row.get<double>("book_age_ms", 50);
        value.reference_quorum_met = row.get<bool>("reference_quorum_met", true);
        value.reference_block_reason = row.get<std::string>("reference_block_reason", "");
        value.target_depth_ok = row.get<bool>("target_depth_ok", true);
        value.probability_block_reason = row.get<std::string>("probability_block_reason", "");
        const auto output = value.strategy == "late_window_directional_ev"
            ? strategy::evaluate_directional(value) : strategy::evaluate_lottery(value);
        std::cout << "{\"decision\":\"" << output.decision << "\",\"reason\":\""
                  << output.reason << "\",\"gross_edge\":";
        write_optional(output.gross_edge);
        std::cout << ",\"net_ev\":";
        write_optional(output.net_ev);
        std::cout << "}\n";
    }
}
