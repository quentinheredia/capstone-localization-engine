#include "telnet_parser.h"
#include <sstream>
#include <regex>
#include <algorithm>

namespace capstone {

// Precompiled regex for MAC address at start of line
static const std::regex MAC_RE(R"(^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})");

static inline std::string rtrim(const std::string& s) {
    auto end = s.find_last_not_of(" \t\r\n");
    return (end == std::string::npos) ? "" : s.substr(0, end + 1);
}

static inline bool starts_with(const std::string& s, const char* prefix) {
    return s.rfind(prefix, 0) == 0;
}

std::vector<APScanRow> parse_apscan_table(const std::string& text) {
    std::vector<APScanRow> rows;
    std::istringstream stream(text);
    std::string line;

    while (std::getline(stream, line)) {
        line = rtrim(line);
        if (line.empty()) continue;

        // Skip headers & noise (mirrors OG parse_apscan.py)
        if (starts_with(line, "2.4G Scanning") ||
            starts_with(line, "Please wait")   ||
            starts_with(line, "ath0")           ||
            starts_with(line, "BSSID"))
            continue;

        // Must begin with a MAC address
        std::smatch m;
        if (!std::regex_search(line, m, MAC_RE))
            continue;

        std::string bssid = m[0].str();

        // Rest of the line after BSSID
        std::string rest = line.substr(bssid.size());
        auto pos = rest.find_first_not_of(" \t");
        if (pos != std::string::npos)
            rest = rest.substr(pos);
        else
            continue;

        // Tokenize
        std::vector<std::string> parts;
        {
            std::istringstream ts(rest);
            std::string tok;
            while (ts >> tok)
                parts.push_back(tok);
        }

        // Need at least 6 fixed columns: LEN MODE CH SIGNAL ENC TYPE
        if (parts.size() < 6)
            continue;

        const size_t n = parts.size();

        APScanRow row;
        row.bssid  = bssid;
        row.type   = parts[n - 1];
        row.enc    = parts[n - 2];
        row.signal = parts[n - 3];
        row.ch     = parts[n - 4];
        row.mode   = parts[n - 5];
        row.len    = parts[n - 6];

        // Everything before the last 6 tokens is the SSID
        std::string ssid;
        for (size_t i = 0; i + 6 < n; ++i) {
            if (!ssid.empty()) ssid += ' ';
            ssid += parts[i];
        }
        row.ssid = ssid;

        rows.push_back(std::move(row));
    }
    return rows;
}

std::vector<std::unordered_map<std::string, std::string>>
parse_apscan_table_dicts(const std::string& text) {
    auto rows = parse_apscan_table(text);
    std::vector<std::unordered_map<std::string, std::string>> out;
    out.reserve(rows.size());

    for (auto& r : rows) {
        out.push_back({
            {"bssid",  std::move(r.bssid)},
            {"ssid",   std::move(r.ssid)},
            {"len",    std::move(r.len)},
            {"mode",   std::move(r.mode)},
            {"ch",     std::move(r.ch)},
            {"signal", std::move(r.signal)},
            {"enc",    std::move(r.enc)},
            {"type",   std::move(r.type)}
        });
    }
    return out;
}

} // namespace capstone
