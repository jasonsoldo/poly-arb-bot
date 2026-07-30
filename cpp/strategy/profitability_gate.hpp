#pragma once

#include <algorithm>
#include <cmath>
#include <iomanip>
#include <iterator>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>

namespace profitability_gate {

struct Candidate {
    std::string strategy;
    std::string asset;
    std::string timeframe;
    std::string outcome;
    double calibration_input_probability = 0;
    double expected_fill_price = 0;
    double seconds_to_close = 0;
};

struct Result {
    std::string decision = "BLOCK";
    std::string reason = "profitability_gate_unavailable";
    std::string cohort_key;
};

inline std::string decile_bucket(double value) {
    if (!std::isfinite(value) || value < 0 || value > 1)
        throw std::invalid_argument("bucket value must be finite and between 0 and 1");
    const int index = std::min(9, static_cast<int>(value * 10));
    std::ostringstream output;
    output << std::fixed << std::setprecision(1)
           << index / 10.0 << '-' << (index + 1) / 10.0;
    return output.str();
}

inline std::string seconds_bucket(double value) {
    if (!std::isfinite(value) || value < 0)
        throw std::invalid_argument("seconds_to_close must be finite and non-negative");
    static const double boundaries[] = {0, 30, 60, 90, 180, 300, 600};
    for (std::size_t index = 0; index + 1 < std::size(boundaries); ++index) {
        if (value >= boundaries[index] && value < boundaries[index + 1]) {
            return std::to_string(static_cast<int>(boundaries[index])) + "-" +
                std::to_string(static_cast<int>(boundaries[index + 1]));
        }
    }
    return "600-inf";
}

inline std::string cohort_key(const Candidate& candidate) {
    if (candidate.strategy.empty() || candidate.asset.empty() ||
            candidate.timeframe.empty() || candidate.outcome.empty())
        throw std::invalid_argument("profitability cohort identity is incomplete");
    return "strategy=" + candidate.strategy +
        "|asset=" + candidate.asset +
        "|timeframe=" + candidate.timeframe +
        "|outcome=" + candidate.outcome +
        "|probability=" + decile_bucket(candidate.calibration_input_probability) +
        "|fill=" + decile_bucket(candidate.expected_fill_price) +
        "|seconds=" + seconds_bucket(candidate.seconds_to_close);
}

inline Result evaluate(
        const Candidate& candidate,
        bool artifacts_ready,
        const std::set<std::string>& eligible_cohorts) {
    Result result;
    try {
        result.cohort_key = cohort_key(candidate);
    } catch (const std::invalid_argument&) {
        return result;
    }
    if (!artifacts_ready) return result;
    if (!eligible_cohorts.count(result.cohort_key)) {
        result.reason = "profitability_cohort_not_eligible";
        return result;
    }
    result.decision = "ALLOW";
    result.reason = "profitability_cohort_eligible";
    return result;
}

}  // namespace profitability_gate
