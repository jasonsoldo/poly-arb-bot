#include <cmath>
#include <iostream>
#include <sstream>
#include <string>
#include <vector>

struct Row {
    std::string market_id;
    std::string title;
    double up_shares = 0.0;
    double up_cost = 0.0;
    double down_shares = 0.0;
    double down_cost = 0.0;
};

static std::vector<std::string> split_tsv_line(const std::string& line) {
    std::vector<std::string> cells;
    std::string cell;
    std::stringstream ss(line);
    while (std::getline(ss, cell, '\t')) {
        cells.push_back(cell);
    }
    return cells;
}

static double parse_double(const std::string& value) {
    try {
        return std::stod(value);
    } catch (...) {
        return 0.0;
    }
}

static std::string classify(double pnl_if_up, double pnl_if_down) {
    if (pnl_if_up > 0.0 && pnl_if_down > 0.0) {
        return "both_profit";
    }
    if (pnl_if_up > 0.0 || pnl_if_down > 0.0) {
        return "one_side_profit";
    }
    return "both_loss";
}

int main() {
    std::ios::sync_with_stdio(false);

    std::string line;
    bool first = true;
    std::cout << "market_id\ttitle\ttotal_cost\tpnl_if_up\tpnl_if_down\tclassification\n";

    while (std::getline(std::cin, line)) {
        if (line.empty()) {
            continue;
        }
        if (first) {
            first = false;
            continue;
        }

        std::vector<std::string> cells = split_tsv_line(line);
        if (cells.size() < 6) {
            continue;
        }

        Row row;
        row.market_id = cells[0];
        row.title = cells[1];
        row.up_shares = parse_double(cells[2]);
        row.up_cost = parse_double(cells[3]);
        row.down_shares = parse_double(cells[4]);
        row.down_cost = parse_double(cells[5]);

        const double total_cost = row.up_cost + row.down_cost;
        const double pnl_if_up = row.up_shares - total_cost;
        const double pnl_if_down = row.down_shares - total_cost;

        std::cout << row.market_id << "\t"
                  << row.title << "\t"
                  << total_cost << "\t"
                  << pnl_if_up << "\t"
                  << pnl_if_down << "\t"
                  << classify(pnl_if_up, pnl_if_down) << "\n";
    }

    return 0;
}
