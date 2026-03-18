#pragma once

#include <string>
#include <vector>
#include <unordered_map>

namespace capstone {

/// A single parsed row from the EAP350 APSCAN table.
struct APScanRow {
    std::string bssid;
    std::string ssid;
    std::string len;
    std::string mode;
    std::string ch;
    std::string signal;
    std::string enc;
    std::string type;
};

/// Parse raw EAP350 APSCAN text output into structured rows.
/// Handles blank SSIDs, SSIDs with spaces, variable whitespace, etc.
std::vector<APScanRow> parse_apscan_table(const std::string& text);

/// Convenience: returns vector of Python-friendly maps (same keys as OG).
std::vector<std::unordered_map<std::string, std::string>>
    parse_apscan_table_dicts(const std::string& text);

} // namespace capstone
