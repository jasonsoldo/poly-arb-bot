#include "profitability_gate.hpp"

#include <fstream>
#include <iostream>
#include <iterator>
#include <map>
#include <stdexcept>
#include <string>

namespace {

std::string read_file(const std::string& path) {
    std::ifstream input(path, std::ios::binary);
    if (!input) throw std::runtime_error("artifact unavailable");
    return std::string(
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>());
}

profitability_gate::ArtifactExpectations expectations(
        const char* cohort_version,
        const char* directional_hash,
        const char* lottery_hash) {
    profitability_gate::ArtifactExpectations expected;
    expected.profitability_cohort_version = cohort_version;
    expected.strategy_base_hashes = {
        {"late_window_directional_ev", directional_hash},
        {"low_price_lottery_ev", lottery_hash},
    };
    expected.probability_model_ids = {
        {"late_window_directional_ev", "directional_logistic_projected_v2"},
        {"low_price_lottery_ev", "lottery_logistic_projected_blend_v2"},
    };
    return expected;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 3 && std::string(argv[1]) == "canonical") {
        const std::string encoded = read_file(argv[2]);
        std::cout << profitability_gate::canonical_payload(encoded) << '\n'
                  << profitability_gate::canonical_payload_hash(encoded)
                  << '\n';
        return 0;
    }
    if ((argc != 8 && argc != 9) ||
            std::string(argv[1]) != "validate") return 2;
    const double now = std::stod(argv[4]);
    const double evaluation_now = argc == 9 ? std::stod(argv[8]) : now;
    const auto artifacts = profitability_gate::validate_artifacts(
        read_file(argv[2]),
        read_file(argv[3]),
        now,
        expectations(argv[5], argv[6], argv[7]));
    std::cout << (artifacts.ready ? "READY" : "BLOCKED") << '\n'
              << artifacts.reason << '\n';
    profitability_gate::Candidate candidate{
        "late_window_directional_ev", "BTC", "5m", "Up", 0.85, 0.45, 45,
    };
    const auto result =
        profitability_gate::evaluate(candidate, artifacts, evaluation_now);
    std::cout << result.decision << '\n' << result.reason << '\n'
              << result.cohort_key << '\n';
    return 0;
}
