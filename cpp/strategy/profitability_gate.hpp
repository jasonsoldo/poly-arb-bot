#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <iomanip>
#include <iterator>
#include <limits>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

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

struct ArtifactExpectations {
    std::string profitability_cohort_version;
    std::map<std::string, std::string> strategy_base_hashes;
    std::map<std::string, std::string> probability_model_ids;
};

struct Artifacts {
    bool ready = false;
    std::string reason = "profitability_gate_unavailable";
    std::string gate_hash;
    std::string calibration_snapshot_hash;
    double snapshot_activated_at = 0;
    double snapshot_expires_at = 0;
    double gate_activated_at = 0;
    double gate_expires_at = 0;
    std::set<std::string> eligible_cohorts;
    std::map<std::string, std::string> target_base_hashes;
    std::map<std::string, std::string> probability_model_ids;
};

namespace detail {

struct Json {
    enum class Type { Null, Bool, Number, String, Array, Object };
    Type type = Type::Null;
    bool boolean = false;
    bool integer = false;
    double number = 0;
    std::string number_token;
    std::u32string string;
    std::vector<Json> array;
    std::map<std::u32string, Json> object;
};

inline void append_utf8(std::u32string& output, std::uint32_t codepoint) {
    if (codepoint > 0x10ffff ||
            (codepoint >= 0xd800 && codepoint <= 0xdfff))
        throw std::invalid_argument("invalid JSON unicode");
    output.push_back(static_cast<char32_t>(codepoint));
}

class JsonParser {
public:
    explicit JsonParser(const std::string& input) : input_(input) {}

    Json parse() {
        skip_space();
        Json value = parse_value();
        skip_space();
        if (position_ != input_.size())
            throw std::invalid_argument("trailing JSON content");
        return value;
    }

private:
    Json parse_value() {
        if (position_ >= input_.size())
            throw std::invalid_argument("unexpected JSON EOF");
        const char token = input_[position_];
        if (token == '{') return parse_object();
        if (token == '[') return parse_array();
        if (token == '"') {
            Json value;
            value.type = Json::Type::String;
            value.string = parse_string();
            return value;
        }
        if (token == 't') return parse_literal("true", true);
        if (token == 'f') return parse_literal("false", false);
        if (token == 'n') {
            require_literal("null");
            return Json{};
        }
        return parse_number();
    }

    Json parse_object() {
        Json value;
        value.type = Json::Type::Object;
        ++position_;
        skip_space();
        if (consume('}')) return value;
        while (true) {
            if (position_ >= input_.size() || input_[position_] != '"')
                throw std::invalid_argument("JSON object key is not a string");
            std::u32string key = parse_string();
            skip_space();
            require(':');
            skip_space();
            Json child = parse_value();
            if (!value.object.emplace(
                    std::move(key), std::move(child)).second)
                throw std::invalid_argument(
                    "duplicate JSON object key");
            skip_space();
            if (consume('}')) break;
            require(',');
            skip_space();
        }
        return value;
    }

    Json parse_array() {
        Json value;
        value.type = Json::Type::Array;
        ++position_;
        skip_space();
        if (consume(']')) return value;
        while (true) {
            value.array.push_back(parse_value());
            skip_space();
            if (consume(']')) break;
            require(',');
            skip_space();
        }
        return value;
    }

    std::uint32_t parse_hex4() {
        if (position_ + 4 > input_.size())
            throw std::invalid_argument("short JSON unicode escape");
        std::uint32_t result = 0;
        for (int index = 0; index < 4; ++index) {
            const char character = input_[position_++];
            result <<= 4;
            if (character >= '0' && character <= '9')
                result |= static_cast<std::uint32_t>(character - '0');
            else if (character >= 'a' && character <= 'f')
                result |= static_cast<std::uint32_t>(character - 'a' + 10);
            else if (character >= 'A' && character <= 'F')
                result |= static_cast<std::uint32_t>(character - 'A' + 10);
            else
                throw std::invalid_argument("invalid JSON unicode escape");
        }
        return result;
    }

    std::u32string parse_string() {
        require('"');
        std::u32string output;
        while (position_ < input_.size()) {
            const unsigned char character =
                static_cast<unsigned char>(input_[position_++]);
            if (character == '"') return output;
            if (character == '\\') {
                if (position_ >= input_.size())
                    throw std::invalid_argument("short JSON escape");
                const char escaped = input_[position_++];
                switch (escaped) {
                    case '"': output.push_back(U'"'); break;
                    case '\\': output.push_back(U'\\'); break;
                    case '/': output.push_back(U'/'); break;
                    case 'b': output.push_back(U'\b'); break;
                    case 'f': output.push_back(U'\f'); break;
                    case 'n': output.push_back(U'\n'); break;
                    case 'r': output.push_back(U'\r'); break;
                    case 't': output.push_back(U'\t'); break;
                    case 'u': {
                        std::uint32_t codepoint = parse_hex4();
                        if (codepoint >= 0xd800 && codepoint <= 0xdbff) {
                            if (position_ + 2 > input_.size() ||
                                    input_[position_] != '\\' ||
                                    input_[position_ + 1] != 'u')
                                throw std::invalid_argument(
                                    "missing JSON low surrogate");
                            position_ += 2;
                            const std::uint32_t low = parse_hex4();
                            if (low < 0xdc00 || low > 0xdfff)
                                throw std::invalid_argument(
                                    "invalid JSON low surrogate");
                            codepoint = 0x10000 +
                                ((codepoint - 0xd800) << 10) +
                                (low - 0xdc00);
                        }
                        append_utf8(output, codepoint);
                        break;
                    }
                    default:
                        throw std::invalid_argument("invalid JSON escape");
                }
                continue;
            }
            if (character < 0x20)
                throw std::invalid_argument("JSON control character");
            if (character < 0x80) {
                output.push_back(static_cast<char32_t>(character));
                continue;
            }
            std::uint32_t codepoint = 0;
            int continuation = 0;
            if ((character & 0xe0) == 0xc0) {
                codepoint = character & 0x1f;
                continuation = 1;
            } else if ((character & 0xf0) == 0xe0) {
                codepoint = character & 0x0f;
                continuation = 2;
            } else if ((character & 0xf8) == 0xf0) {
                codepoint = character & 0x07;
                continuation = 3;
            } else {
                throw std::invalid_argument("invalid JSON UTF-8");
            }
            for (int index = 0; index < continuation; ++index) {
                if (position_ >= input_.size())
                    throw std::invalid_argument("short JSON UTF-8");
                const unsigned char next =
                    static_cast<unsigned char>(input_[position_++]);
                if ((next & 0xc0) != 0x80)
                    throw std::invalid_argument("invalid JSON UTF-8");
                codepoint = (codepoint << 6) | (next & 0x3f);
            }
            append_utf8(output, codepoint);
        }
        throw std::invalid_argument("unterminated JSON string");
    }

    Json parse_number() {
        const std::size_t start = position_;
        if (consume('-') && position_ >= input_.size())
            throw std::invalid_argument("short JSON number");
        if (consume('0')) {
            if (position_ < input_.size() &&
                    input_[position_] >= '0' && input_[position_] <= '9')
                throw std::invalid_argument("JSON number has leading zero");
        } else {
            require_digit();
            while (is_digit()) ++position_;
        }
        bool integer = true;
        if (consume('.')) {
            integer = false;
            require_digit();
            while (is_digit()) ++position_;
        }
        if (position_ < input_.size() &&
                (input_[position_] == 'e' || input_[position_] == 'E')) {
            integer = false;
            ++position_;
            if (position_ < input_.size() &&
                    (input_[position_] == '+' || input_[position_] == '-'))
                ++position_;
            require_digit();
            while (is_digit()) ++position_;
        }
        Json value;
        value.type = Json::Type::Number;
        value.integer = integer;
        value.number_token = input_.substr(start, position_ - start);
        char* end = nullptr;
        value.number = std::strtod(value.number_token.c_str(), &end);
        if (!end || *end || !std::isfinite(value.number))
            throw std::invalid_argument("invalid JSON number");
        return value;
    }

    Json parse_literal(const char* literal, bool boolean) {
        require_literal(literal);
        Json value;
        value.type = Json::Type::Bool;
        value.boolean = boolean;
        return value;
    }

    void require_literal(const char* literal) {
        const std::size_t length = std::strlen(literal);
        if (input_.compare(position_, length, literal) != 0)
            throw std::invalid_argument("invalid JSON literal");
        position_ += length;
    }

    void skip_space() {
        while (position_ < input_.size() &&
                (input_[position_] == ' ' || input_[position_] == '\t' ||
                 input_[position_] == '\r' || input_[position_] == '\n'))
            ++position_;
    }

    bool consume(char token) {
        if (position_ < input_.size() && input_[position_] == token) {
            ++position_;
            return true;
        }
        return false;
    }

    void require(char token) {
        if (!consume(token))
            throw std::invalid_argument("missing JSON delimiter");
    }

    bool is_digit() const {
        return position_ < input_.size() &&
            input_[position_] >= '0' && input_[position_] <= '9';
    }

    void require_digit() {
        if (!is_digit()) throw std::invalid_argument("invalid JSON number");
    }

    const std::string& input_;
    std::size_t position_ = 0;
};

inline void append_hex_escape(std::string& output, std::uint32_t value) {
    static const char digits[] = "0123456789abcdef";
    output += "\\u";
    for (int shift = 12; shift >= 0; shift -= 4)
        output += digits[(value >> shift) & 0xf];
}

inline void append_string(std::string& output, const std::u32string& value) {
    output += '"';
    for (std::uint32_t codepoint : value) {
        switch (codepoint) {
            case '"': output += "\\\""; break;
            case '\\': output += "\\\\"; break;
            case '\b': output += "\\b"; break;
            case '\f': output += "\\f"; break;
            case '\n': output += "\\n"; break;
            case '\r': output += "\\r"; break;
            case '\t': output += "\\t"; break;
            default:
                if (codepoint < 0x20 || codepoint >= 0x80) {
                    if (codepoint <= 0xffff) {
                        append_hex_escape(output, codepoint);
                    } else {
                        const std::uint32_t adjusted = codepoint - 0x10000;
                        append_hex_escape(output, 0xd800 + (adjusted >> 10));
                        append_hex_escape(output, 0xdc00 + (adjusted & 0x3ff));
                    }
                } else {
                    output += static_cast<char>(codepoint);
                }
        }
    }
    output += '"';
}

inline void append_json(std::string& output, const Json& value) {
    switch (value.type) {
        case Json::Type::Null: output += "null"; return;
        case Json::Type::Bool:
            output += value.boolean ? "true" : "false";
            return;
        case Json::Type::Number:
            output += value.number_token;
            return;
        case Json::Type::String:
            append_string(output, value.string);
            return;
        case Json::Type::Array: {
            output += '[';
            bool first = true;
            for (const auto& item : value.array) {
                if (!first) output += ',';
                first = false;
                append_json(output, item);
            }
            output += ']';
            return;
        }
        case Json::Type::Object: {
            output += '{';
            bool first = true;
            for (const auto& item : value.object) {
                if (!first) output += ',';
                first = false;
                append_string(output, item.first);
                output += ':';
                append_json(output, item.second);
            }
            output += '}';
        }
    }
}

inline std::u32string unicode(const std::string& value) {
    std::string encoded = "\"";
    for (unsigned char character : value) {
        if (character == '"' || character == '\\') encoded += '\\';
        encoded += static_cast<char>(character);
    }
    encoded += '"';
    return JsonParser(encoded).parse().string;
}

inline std::string utf8(const std::u32string& value) {
    std::string output;
    for (std::uint32_t codepoint : value) {
        if (codepoint <= 0x7f) {
            output += static_cast<char>(codepoint);
        } else if (codepoint <= 0x7ff) {
            output += static_cast<char>(0xc0 | (codepoint >> 6));
            output += static_cast<char>(0x80 | (codepoint & 0x3f));
        } else if (codepoint <= 0xffff) {
            output += static_cast<char>(0xe0 | (codepoint >> 12));
            output += static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f));
            output += static_cast<char>(0x80 | (codepoint & 0x3f));
        } else {
            output += static_cast<char>(0xf0 | (codepoint >> 18));
            output += static_cast<char>(0x80 | ((codepoint >> 12) & 0x3f));
            output += static_cast<char>(0x80 | ((codepoint >> 6) & 0x3f));
            output += static_cast<char>(0x80 | (codepoint & 0x3f));
        }
    }
    return output;
}

inline const Json* field(const Json& object, const std::string& name) {
    if (object.type != Json::Type::Object) return nullptr;
    const auto found = object.object.find(unicode(name));
    return found == object.object.end() ? nullptr : &found->second;
}

inline std::string string_value(const Json* value) {
    return value && value->type == Json::Type::String
        ? utf8(value->string) : std::string();
}

inline bool exact_fields(
        const Json& value, const std::set<std::string>& names) {
    if (value.type != Json::Type::Object ||
            value.object.size() != names.size())
        return false;
    for (const auto& item : value.object)
        if (!names.count(utf8(item.first))) return false;
    return true;
}

inline bool number_value(const Json* value, double& output) {
    if (!value || value->type != Json::Type::Number ||
            !std::isfinite(value->number))
        return false;
    output = value->number;
    return true;
}

inline bool integer_value(const Json* value, long long& output) {
    if (!value || value->type != Json::Type::Number || !value->integer ||
            !std::isfinite(value->number) ||
            value->number < static_cast<double>(
                std::numeric_limits<long long>::min()) ||
            value->number > static_cast<double>(
                std::numeric_limits<long long>::max()))
        return false;
    char* end = nullptr;
    output = std::strtoll(value->number_token.c_str(), &end, 10);
    return end && !*end;
}

inline std::uint32_t rotate_right(std::uint32_t value, int count) {
    return (value >> count) | (value << (32 - count));
}

inline std::string sha256(const std::string& input) {
    static const std::uint32_t constants[64] = {
        0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5,
        0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
        0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
        0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
        0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc,
        0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
        0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7,
        0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
        0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
        0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
        0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3,
        0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
        0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5,
        0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
        0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
        0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
    };
    std::array<std::uint32_t, 8> state{{
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
        0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    }};
    std::vector<unsigned char> data(input.begin(), input.end());
    const std::uint64_t bit_length =
        static_cast<std::uint64_t>(data.size()) * 8;
    data.push_back(0x80);
    while ((data.size() % 64) != 56) data.push_back(0);
    for (int shift = 56; shift >= 0; shift -= 8)
        data.push_back(static_cast<unsigned char>(bit_length >> shift));
    for (std::size_t offset = 0; offset < data.size(); offset += 64) {
        std::uint32_t words[64] = {};
        for (int index = 0; index < 16; ++index) {
            const std::size_t at = offset + index * 4;
            words[index] =
                (static_cast<std::uint32_t>(data[at]) << 24) |
                (static_cast<std::uint32_t>(data[at + 1]) << 16) |
                (static_cast<std::uint32_t>(data[at + 2]) << 8) |
                static_cast<std::uint32_t>(data[at + 3]);
        }
        for (int index = 16; index < 64; ++index) {
            const std::uint32_t s0 =
                rotate_right(words[index - 15], 7) ^
                rotate_right(words[index - 15], 18) ^
                (words[index - 15] >> 3);
            const std::uint32_t s1 =
                rotate_right(words[index - 2], 17) ^
                rotate_right(words[index - 2], 19) ^
                (words[index - 2] >> 10);
            words[index] = words[index - 16] + s0 +
                words[index - 7] + s1;
        }
        std::uint32_t a = state[0], b = state[1], c = state[2],
            d = state[3], e = state[4], f = state[5],
            g = state[6], h = state[7];
        for (int index = 0; index < 64; ++index) {
            const std::uint32_t sigma1 =
                rotate_right(e, 6) ^ rotate_right(e, 11) ^
                rotate_right(e, 25);
            const std::uint32_t choose = (e & f) ^ (~e & g);
            const std::uint32_t temporary1 =
                h + sigma1 + choose + constants[index] + words[index];
            const std::uint32_t sigma0 =
                rotate_right(a, 2) ^ rotate_right(a, 13) ^
                rotate_right(a, 22);
            const std::uint32_t majority =
                (a & b) ^ (a & c) ^ (b & c);
            const std::uint32_t temporary2 = sigma0 + majority;
            h = g; g = f; f = e; e = d + temporary1;
            d = c; c = b; b = a; a = temporary1 + temporary2;
        }
        state[0] += a; state[1] += b; state[2] += c; state[3] += d;
        state[4] += e; state[5] += f; state[6] += g; state[7] += h;
    }
    std::ostringstream output;
    output << std::hex << std::setfill('0');
    for (std::uint32_t value : state) output << std::setw(8) << value;
    return output.str();
}

inline bool hash_is_valid(const std::string& value) {
    if (value.size() != 64) return false;
    return std::all_of(value.begin(), value.end(), [](char character) {
        return (character >= '0' && character <= '9') ||
            (character >= 'a' && character <= 'f');
    });
}

inline bool probability_bucket_name(const std::string& name) {
    static const std::set<std::string> names = {
        "0.0-0.1", "0.1-0.2", "0.2-0.3", "0.3-0.4", "0.4-0.5",
        "0.5-0.6", "0.6-0.7", "0.7-0.8", "0.8-0.9", "0.9-1.0",
    };
    return names.count(name) != 0;
}

inline bool seconds_bucket_name(const std::string& name) {
    static const std::set<std::string> names = {
        "0-30", "30-60", "60-90", "90-180",
        "180-300", "300-600", "600-inf",
    };
    return names.count(name) != 0;
}

inline bool probability_strategy(const std::string& strategy) {
    return strategy == "late_window_directional_ev" ||
        strategy == "low_price_lottery_ev";
}

inline bool validate_source_identity(const Json& source) {
    if (!exact_fields(
            source, {"strategy_audit", "execution_log"}))
        return false;
    for (const std::string side : {
             "strategy_audit", "execution_log",
         }) {
        const Json* identity = field(source, side);
        if (!identity ||
                !exact_fields(*identity, {"path", "files"}) ||
                string_value(field(*identity, "path")).empty())
            return false;
        const Json* files = field(*identity, "files");
        if (!files || files->type != Json::Type::Array)
            return false;
        for (const Json& item : files->array) {
            long long bytes = -1;
            if (!exact_fields(item, {"path", "bytes", "sha256"}) ||
                    string_value(field(item, "path")).empty() ||
                    !integer_value(field(item, "bytes"), bytes) ||
                    bytes < 0 ||
                    !hash_is_valid(
                        string_value(field(item, "sha256"))))
                return false;
        }
    }
    return true;
}

inline bool validate_bucket_map(
        const Json& buckets, long long minimum_samples,
        bool require_usable) {
    if (buckets.type != Json::Type::Object || buckets.object.empty())
        return false;
    bool usable = false;
    for (const auto& item : buckets.object) {
        if (!probability_bucket_name(utf8(item.first)) ||
                !exact_fields(item.second, {
                    "samples", "expected_up_rate", "realized_up_rate",
                }))
            return false;
        long long samples = 0;
        double expected = 0, realized = 0;
        if (!integer_value(field(item.second, "samples"), samples) ||
                samples <= 0 ||
                !number_value(
                    field(item.second, "expected_up_rate"), expected) ||
                !number_value(
                    field(item.second, "realized_up_rate"), realized) ||
                expected < 0 || expected > 1 ||
                realized < 0 || realized > 1)
            return false;
        usable = usable || samples >= minimum_samples;
    }
    return !require_usable || usable;
}

inline bool validate_dimensions(
        const Json& dimensions, std::string* key = nullptr,
        bool allow_unknown_strategy = false) {
    static const std::vector<std::string> names = {
        "strategy", "asset", "timeframe", "outcome",
        "probability", "fill", "seconds",
    };
    if (!exact_fields(dimensions, {
            "strategy", "asset", "timeframe", "outcome",
            "probability", "fill", "seconds",
        }))
        return false;
    std::ostringstream output;
    for (std::size_t index = 0; index < names.size(); ++index) {
        const std::string value =
            string_value(field(dimensions, names[index]));
        if (value.empty()) return false;
        if (index) output << '|';
        output << names[index] << '=' << value;
    }
    const std::string strategy =
        string_value(field(dimensions, "strategy"));
    const std::string probability =
        string_value(field(dimensions, "probability"));
    const std::string fill = string_value(field(dimensions, "fill"));
    const std::string seconds = string_value(field(dimensions, "seconds"));
    if ((!allow_unknown_strategy && !probability_strategy(strategy)) ||
            !probability_bucket_name(probability) ||
            !probability_bucket_name(fill) ||
            !seconds_bucket_name(seconds))
        return false;
    if (key) *key = output.str();
    return true;
}

inline std::string cohort_rejection(const Json& entry) {
    if (entry.type != Json::Type::Object) return "invalid_cohort";
    long long markets = 0;
    if (!integer_value(field(entry, "independent_markets"), markets) ||
            markets < 50)
        return "insufficient_independent_markets";
    double mean = 0;
    if (!number_value(field(entry, "mean_net_return"), mean) || mean <= 0)
        return "mean_net_return_not_positive";
    double lower = 0;
    if (!number_value(field(entry, "lower_bound_95"), lower) || lower <= 0)
        return "lower_bound_95_not_positive";
    double share = 0;
    if (!number_value(
            field(entry, "largest_positive_market_share"), share) ||
            share < 0 || share > 0.25)
        return "positive_pnl_too_concentrated";
    return "";
}

inline bool validate_snapshot(
        const Json& snapshot, double now,
        const ArtifactExpectations& expected,
        Artifacts& artifacts) {
    long long version = 0;
    double generated = 0, activated = 0, expires = 0;
    if (!exact_fields(snapshot, {
            "version", "generated_at", "config",
            "excluded_other_cohort", "strategies",
            "validation_activated_at", "validation_expires_at",
            "content_hash",
        }) ||
            !integer_value(field(snapshot, "version"), version) ||
            version != 2 ||
            !number_value(field(snapshot, "generated_at"), generated) ||
            generated <= 0 ||
            !number_value(
                field(snapshot, "validation_activated_at"), activated) ||
            !number_value(
                field(snapshot, "validation_expires_at"), expires) ||
            generated > activated || now < activated ||
            expires != activated + 72 * 3600 || now >= expires)
        return false;
    const Json* config = field(snapshot, "config");
    if (!config || !exact_fields(
            *config, {"min_bucket_samples", "prior_weight"}))
        return false;
    long long minimum_samples = 0;
    double prior_weight = 0;
    if (!integer_value(
            field(*config, "min_bucket_samples"), minimum_samples) ||
            minimum_samples <= 0 ||
            !number_value(field(*config, "prior_weight"), prior_weight) ||
            prior_weight < 0)
        return false;
    const Json* excluded = field(snapshot, "excluded_other_cohort");
    const Json* strategies = field(snapshot, "strategies");
    if (!excluded || !strategies ||
            excluded->type != Json::Type::Object ||
            strategies->type != Json::Type::Object ||
            excluded->object.size() != expected.strategy_base_hashes.size() ||
            strategies->object.size() != expected.strategy_base_hashes.size())
        return false;
    for (const auto& identity : expected.strategy_base_hashes) {
        long long excluded_count = 0;
        if (!integer_value(
                field(*excluded, identity.first), excluded_count) ||
                excluded_count < 0)
            return false;
        const Json* entry = field(*strategies, identity.first);
        if (!entry || !exact_fields(
                *entry, {"cohort", "timeframes", "overall"}))
            return false;
        const Json* cohort = field(*entry, "cohort");
        const Json* timeframes = field(*entry, "timeframes");
        const Json* overall = field(*entry, "overall");
        const auto model = expected.probability_model_ids.find(identity.first);
        if (!cohort || !exact_fields(
                *cohort, {"strategy_config_hash", "probability_model_id"}) ||
                string_value(field(*cohort, "strategy_config_hash")) !=
                    identity.second ||
                model == expected.probability_model_ids.end() ||
                string_value(field(*cohort, "probability_model_id")) !=
                    model->second ||
                !timeframes ||
                timeframes->type != Json::Type::Object ||
                timeframes->object.empty() || !overall ||
                !validate_bucket_map(
                    *overall, minimum_samples, true))
            return false;
        for (const auto& timeframe : timeframes->object) {
            if (timeframe.first.empty() ||
                    !validate_bucket_map(
                        timeframe.second, minimum_samples, false))
                return false;
        }
    }
    artifacts.snapshot_activated_at = activated;
    artifacts.snapshot_expires_at = expires;
    return true;
}

inline bool validate_gate(
        const Json& gate, double now,
        const ArtifactExpectations& expected,
        Artifacts& artifacts) {
    long long version = 0;
    double activated = 0, expires = 0;
    const Json* eligible = field(gate, "eligible_cohorts");
    const Json* rejected = field(gate, "rejected_cohorts");
    const Json* targets = field(gate, "target_base_config_hashes");
    const Json* sources = field(gate, "source_discovery_config_hashes");
    const Json* models = field(gate, "probability_model_ids");
    const Json* thresholds = field(gate, "thresholds");
    if (!exact_fields(gate, {
            "version", "generated_at",
            "validation_activated_at", "validation_expires_at",
            "profitability_cohort_version",
            "calibration_snapshot_hash", "source",
            "source_discovery_config_hashes",
            "target_base_config_hashes", "probability_model_ids",
            "thresholds", "decision", "eligible_cohorts",
            "rejected_cohorts", "real_order_submissions",
            "real_orders", "real_fills", "content_hash",
        }) ||
            !integer_value(field(gate, "version"), version) ||
            version != 1 || !eligible || !rejected || !targets ||
            !sources || !models || !thresholds ||
            eligible->type != Json::Type::Object ||
            rejected->type != Json::Type::Object ||
            targets->type != Json::Type::Object ||
            sources->type != Json::Type::Object ||
            models->type != Json::Type::Object ||
            !field(gate, "source") ||
            !validate_source_identity(*field(gate, "source")) ||
            !exact_fields(*thresholds, {
                "minimum_independent_markets",
                "minimum_mean_net_return_exclusive",
                "minimum_lower_bound_95_exclusive",
                "maximum_positive_market_share",
            }))
        return false;
    long long minimum_markets = 0;
    double minimum_mean = 0, minimum_lower = 0, maximum_share = 0;
    if (!integer_value(
            field(*thresholds, "minimum_independent_markets"),
            minimum_markets) ||
            minimum_markets != 50 ||
            !number_value(
                field(*thresholds, "minimum_mean_net_return_exclusive"),
                minimum_mean) ||
            minimum_mean != 0 ||
            !number_value(
                field(*thresholds, "minimum_lower_bound_95_exclusive"),
                minimum_lower) ||
            minimum_lower != 0 ||
            !number_value(
                field(*thresholds, "maximum_positive_market_share"),
                maximum_share) ||
            maximum_share != 0.25)
        return false;
    const std::string decision =
        string_value(field(gate, "decision"));
    long long real_order_submissions = -1;
    long long real_orders = -1;
    long long real_fills = -1;
    if ((decision != "ALLOW" && decision != "NO_TRADE") ||
            (decision == "ALLOW") != !eligible->object.empty() ||
            (decision == "NO_TRADE" && !eligible->object.empty()) ||
            !integer_value(
                field(gate, "real_order_submissions"),
                real_order_submissions) ||
            !integer_value(field(gate, "real_orders"), real_orders) ||
            !integer_value(field(gate, "real_fills"), real_fills) ||
            real_order_submissions != 0 || real_orders != 0 ||
            real_fills != 0)
        return false;
    artifacts.calibration_snapshot_hash =
        string_value(field(gate, "calibration_snapshot_hash"));
    double generated = 0;
    if (!hash_is_valid(artifacts.calibration_snapshot_hash) ||
            string_value(field(
                gate, "profitability_cohort_version")) !=
                expected.profitability_cohort_version ||
            !number_value(field(gate, "generated_at"), generated) ||
            !number_value(
                field(gate, "validation_activated_at"), activated) ||
            !number_value(field(gate, "validation_expires_at"), expires) ||
            generated <= 0 || generated != activated ||
            activated <= 0 || now < activated || now >= expires ||
            expires <= activated ||
            expires > activated + 72 * 3600)
        return false;
    for (const auto& item : targets->object) {
        const std::string strategy = utf8(item.first);
        const std::string value = string_value(&item.second);
        const auto base = expected.strategy_base_hashes.find(strategy);
        const auto model = expected.probability_model_ids.find(strategy);
        if (!probability_strategy(strategy) || value.empty() ||
                base == expected.strategy_base_hashes.end() ||
                value != base->second ||
                model == expected.probability_model_ids.end())
            return false;
        artifacts.target_base_hashes[strategy] = value;
    }
    if (models->object.size() != targets->object.size()) return false;
    for (const auto& target : artifacts.target_base_hashes) {
        const std::string model =
            string_value(field(*models, target.first));
        if (model.empty() ||
                model != expected.probability_model_ids.at(target.first))
            return false;
        artifacts.probability_model_ids[target.first] = model;
    }
    for (const auto& item : sources->object) {
        const std::string strategy = utf8(item.first);
        const auto target = artifacts.target_base_hashes.find(strategy);
        if (target == artifacts.target_base_hashes.end() ||
                string_value(&item.second) != target->second)
            return false;
    }
    for (const auto& item : eligible->object) {
        const std::string key = utf8(item.first);
        const Json& entry = item.second;
        const Json* dimensions = field(entry, "dimensions");
        std::string dimensions_key;
        double net_pnl = 0;
        if (!exact_fields(entry, {
                "dimensions", "independent_markets",
                "mean_net_return", "net_pnl_usd", "lower_bound_95",
                "largest_positive_market_share", "decision", "reason",
                "source_discovery_config_hash",
                "strategy_base_config_hash", "probability_model_id",
            }) ||
                !dimensions ||
                !validate_dimensions(*dimensions, &dimensions_key) ||
                dimensions_key != key ||
                string_value(field(entry, "decision")) != "ALLOW" ||
                string_value(field(entry, "reason")) !=
                    "profitability_cohort_eligible" ||
                !cohort_rejection(entry).empty() ||
                !number_value(field(entry, "net_pnl_usd"), net_pnl))
            return false;
        const std::string strategy = string_value(
            field(*dimensions, "strategy"));
        const auto target = artifacts.target_base_hashes.find(strategy);
        const auto model = artifacts.probability_model_ids.find(strategy);
        const auto source = sources->object.find(unicode(strategy));
        if (target == artifacts.target_base_hashes.end() ||
                model == artifacts.probability_model_ids.end() ||
                source == sources->object.end() ||
                string_value(field(
                    entry, "source_discovery_config_hash")) !=
                    target->second ||
                string_value(field(
                    entry, "strategy_base_config_hash")) !=
                    target->second ||
                string_value(field(entry, "probability_model_id")) !=
                    model->second)
            return false;
        artifacts.eligible_cohorts.insert(key);
    }
    static const std::set<std::string> rejection_reasons = {
        "invalid_cohort", "insufficient_independent_markets",
        "mean_net_return_not_positive", "lower_bound_95_not_positive",
        "positive_pnl_too_concentrated", "cohort_key_mismatch",
        "unknown_strategy", "target_base_config_hash_unavailable",
        "calibration_cohort_mismatch",
    };
    for (const auto& item : rejected->object) {
        const std::string key = utf8(item.first);
        const Json& entry = item.second;
        const std::string reason = string_value(field(entry, "reason"));
        if (exact_fields(entry, {"source", "decision", "reason"}) &&
                string_value(field(entry, "decision")) == "BLOCK" &&
                reason == "invalid_cohort" &&
                !artifacts.eligible_cohorts.count(key))
            continue;
        if (!exact_fields(entry, {
                "dimensions", "independent_markets",
                "mean_net_return", "net_pnl_usd", "lower_bound_95",
                "largest_positive_market_share", "decision", "reason",
            }) ||
                string_value(field(entry, "decision")) != "BLOCK" ||
                !rejection_reasons.count(reason) ||
                artifacts.eligible_cohorts.count(key))
            return false;
        const Json* dimensions = field(entry, "dimensions");
        std::string dimensions_key;
        if (!dimensions ||
                !validate_dimensions(
                    *dimensions, &dimensions_key,
                    reason == "unknown_strategy"))
            return false;
        if (reason != "cohort_key_mismatch" &&
                dimensions_key != key)
            return false;
        long long rejected_markets = -1;
        double rejected_mean = 0, rejected_pnl = 0;
        double rejected_lower = 0, rejected_share = 0;
        if (!integer_value(
                field(entry, "independent_markets"),
                rejected_markets) ||
                rejected_markets < 0 ||
                !number_value(
                    field(entry, "mean_net_return"), rejected_mean) ||
                !number_value(
                    field(entry, "net_pnl_usd"), rejected_pnl) ||
                !number_value(
                    field(entry, "lower_bound_95"), rejected_lower) ||
                !number_value(
                    field(entry, "largest_positive_market_share"),
                    rejected_share))
            return false;
        const std::string threshold_reason = cohort_rejection(entry);
        if ((reason == "insufficient_independent_markets" ||
             reason == "mean_net_return_not_positive" ||
             reason == "lower_bound_95_not_positive" ||
             reason == "positive_pnl_too_concentrated") &&
                threshold_reason != reason)
            return false;
        if ((reason == "cohort_key_mismatch" ||
             reason == "unknown_strategy" ||
             reason == "target_base_config_hash_unavailable" ||
             reason == "calibration_cohort_mismatch") &&
                !threshold_reason.empty())
            return false;
    }
    artifacts.gate_activated_at = activated;
    artifacts.gate_expires_at = expires;
    return true;
}

}  // namespace detail

inline std::string canonical_payload(const std::string& encoded) {
    detail::Json root = detail::JsonParser(encoded).parse();
    if (root.type != detail::Json::Type::Object)
        throw std::invalid_argument("canonical payload must be an object");
    root.object.erase(detail::unicode("content_hash"));
    std::string output;
    detail::append_json(output, root);
    return output;
}

inline std::string canonical_payload_hash(const std::string& encoded) {
    return detail::sha256(canonical_payload(encoded));
}

inline Artifacts validate_artifacts(
        const std::string& snapshot_encoded,
        const std::string& gate_encoded,
        double now,
        const ArtifactExpectations& expected) {
    Artifacts artifacts;
    if (!std::isfinite(now) ||
            expected.profitability_cohort_version.empty())
        return artifacts;
    try {
        const detail::Json snapshot =
            detail::JsonParser(snapshot_encoded).parse();
        const std::string snapshot_hash =
            canonical_payload_hash(snapshot_encoded);
        if (detail::string_value(
                detail::field(snapshot, "content_hash")) != snapshot_hash ||
                !detail::validate_snapshot(
                    snapshot, now, expected, artifacts))
            return artifacts;
        artifacts.calibration_snapshot_hash = snapshot_hash;
        const detail::Json gate =
            detail::JsonParser(gate_encoded).parse();
        const std::string gate_hash = canonical_payload_hash(gate_encoded);
        if (detail::string_value(
                detail::field(gate, "content_hash")) != gate_hash)
            return artifacts;
        const std::string snapshot_binding =
            detail::string_value(
                detail::field(gate, "calibration_snapshot_hash"));
        if (snapshot_binding != snapshot_hash ||
                !detail::validate_gate(gate, now, expected, artifacts))
            return artifacts;
        if (artifacts.gate_activated_at <
                artifacts.snapshot_activated_at ||
                artifacts.gate_expires_at >
                artifacts.snapshot_expires_at)
            return artifacts;
        artifacts.gate_hash = gate_hash;
        artifacts.ready = true;
        artifacts.reason.clear();
    } catch (const std::exception&) {
        return Artifacts{};
    }
    return artifacts;
}

inline std::string decile_bucket(double value) {
    if (!std::isfinite(value) || value < 0 || value > 1)
        throw std::invalid_argument(
            "bucket value must be finite and between 0 and 1");
    const int index = std::min(9, static_cast<int>(value * 10));
    std::ostringstream output;
    output << std::fixed << std::setprecision(1)
           << index / 10.0 << '-' << (index + 1) / 10.0;
    return output.str();
}

inline std::string seconds_bucket(double value) {
    if (!std::isfinite(value) || value < 0)
        throw std::invalid_argument(
            "seconds_to_close must be finite and non-negative");
    static const double boundaries[] = {0, 30, 60, 90, 180, 300, 600};
    for (std::size_t index = 0;
            index + 1 < std::size(boundaries); ++index) {
        if (value >= boundaries[index] &&
                value < boundaries[index + 1]) {
            return std::to_string(
                static_cast<int>(boundaries[index])) + "-" +
                std::to_string(
                    static_cast<int>(boundaries[index + 1]));
        }
    }
    return "600-inf";
}

inline std::string cohort_key(const Candidate& candidate) {
    if (candidate.strategy.empty() || candidate.asset.empty() ||
            candidate.timeframe.empty() || candidate.outcome.empty())
        throw std::invalid_argument(
            "profitability cohort identity is incomplete");
    return "strategy=" + candidate.strategy +
        "|asset=" + candidate.asset +
        "|timeframe=" + candidate.timeframe +
        "|outcome=" + candidate.outcome +
        "|probability=" +
            decile_bucket(candidate.calibration_input_probability) +
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

inline Result evaluate(
        const Candidate& candidate,
        const Artifacts& artifacts,
        double now) {
    const bool current =
        artifacts.ready && std::isfinite(now) &&
        artifacts.snapshot_activated_at <= now &&
        now < artifacts.snapshot_expires_at &&
        artifacts.gate_activated_at <= now &&
        now < artifacts.gate_expires_at;
    return evaluate(candidate, current, artifacts.eligible_cohorts);
}

}  // namespace profitability_gate
