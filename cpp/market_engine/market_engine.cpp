#include <algorithm>
#include <chrono>
#include <iomanip>
#include <iostream>
#include <sstream>
#include <string>
#include <unordered_map>
#include <vector>

struct Level { double price = 0; double size = 0; };
struct Book { std::vector<Level> bids; std::vector<Level> asks; };
struct Market { std::string up; std::string down; double size = 0; double fee = .07; double active_since = 0; };

static std::vector<std::string> split(const std::string& line) {
    std::vector<std::string> out; std::stringstream ss(line); std::string cell;
    while (std::getline(ss, cell, '\t')) out.push_back(cell);
    return out;
}
static double number(const std::string& value) { try { return std::stod(value); } catch (...) { return 0; } }
static void set_level(std::vector<Level>& levels, double price, double size) {
    auto it = std::find_if(levels.begin(), levels.end(), [price](const Level& x) { return x.price == price; });
    if (size <= 0) { if (it != levels.end()) levels.erase(it); }
    else if (it == levels.end()) levels.push_back({price, size}); else it->size = size;
}
static void sort_book(Book& book) {
    std::sort(book.bids.begin(), book.bids.end(), [](auto a, auto b) { return a.price > b.price; });
    std::sort(book.asks.begin(), book.asks.end(), [](auto a, auto b) { return a.price < b.price; });
}
static std::pair<double, double> buy_vwap(const Book& book, double want) {
    double remaining = want, notional = 0, filled = 0;
    for (const auto& level : book.asks) { double take = std::min(remaining, level.size); notional += take * level.price; filled += take; remaining -= take; if (remaining <= 1e-9) break; }
    return {filled, filled > 0 ? notional / filled : 0};
}
static double now_seconds() { return std::chrono::duration<double>(std::chrono::system_clock::now().time_since_epoch()).count(); }

int main() {
    std::ios::sync_with_stdio(false); std::unordered_map<std::string, Book> books; std::unordered_map<std::string, Market> markets;
    std::string line;
    std::cout << "event_type\tmarket_id\tup_vwap\tdown_vwap\tup_fee\tdown_fee\ttotal_cost\tprofit\tfok\tprofitable\tduration_s\n" << std::flush;
    while (std::getline(std::cin, line)) {
        auto c = split(line); if (c.empty()) continue;
        if (c[0] == "MARKET" && c.size() >= 5) markets[c[1]] = {c[2], c[3], number(c[4]), c.size() > 5 ? number(c[5]) : .07};
        else if (c[0] == "BOOK" && c.size() >= 5) { auto& b = books[c[1]]; auto& side = c[2] == "BUY" ? b.bids : b.asks; side.clear(); set_level(side, number(c[3]), number(c[4])); sort_book(b); }
        else if (c[0] == "CHANGE" && c.size() >= 5) { auto& b = books[c[1]]; auto& side = c[2] == "BUY" ? b.bids : b.asks; set_level(side, number(c[3]), number(c[4])); sort_book(b); }
        else if (c[0] == "EVAL" && c.size() >= 2) {
            auto it = markets.find(c[1]); if (it == markets.end()) continue; const auto& m = it->second;
            auto up = buy_vwap(books[m.up], m.size); auto down = buy_vwap(books[m.down], m.size); bool fok = up.first >= m.size && down.first >= m.size;
            double up_fee = up.first * m.fee * up.second * (1 - up.second), down_fee = down.first * m.fee * down.second * (1 - down.second);
            double total = up.second * m.size + down.second * m.size + up_fee + down_fee; double profit = fok ? m.size - total : 0;
            bool profitable = fok && profit > 0; double ts = now_seconds(); if (profitable && m.active_since == 0) markets[c[1]].active_since = ts;
            if (!profitable) markets[c[1]].active_since = 0;
            std::cout << "shadow_opportunity\t" << c[1] << '\t' << std::setprecision(12) << up.second << '\t' << down.second << '\t' << up_fee << '\t' << down_fee << '\t' << (fok ? total : 0) << '\t' << profit << '\t' << (fok ? 1 : 0) << '\t' << (profitable ? 1 : 0) << '\t' << (profitable ? ts - markets[c[1]].active_since : 0) << '\n' << std::flush;
        }
    }
}
