#pragma once

#include <string>
#include <deque>
#include <unordered_map>

namespace capstone {

/// Stateful rolling-average RSSI filter.
/// Maintains per-AP, per-device history queues (mirrors Python RSSIFilter).
class RSSIFilter {
public:
    RSSIFilter(int window_size, double noise_floor_dbm);

    /// Feed a single raw RSSI reading.  Returns the smoothed value,
    /// or -999.0 if filtered out (below noise floor).
    double feed(const std::string& ap_id,
                const std::string& device_id,
                double raw_rssi);

    /// Batch process: {ap_id: {device_id: raw_rssi}} -> {ap_id: {device_id: smoothed}}.
    /// Entries below noise floor are omitted from the output.
    using RSSIMap = std::unordered_map<std::string,
                       std::unordered_map<std::string, double>>;
    RSSIMap process(const RSSIMap& raw);

    /// Reset all history.
    void clear();

    int window_size() const { return window_size_; }
    double noise_floor() const { return noise_floor_dbm_; }

private:
    int    window_size_;
    double noise_floor_dbm_;

    // history_[ap_id][device_id] = rolling deque of RSSI readings
    std::unordered_map<std::string,
        std::unordered_map<std::string, std::deque<double>>> history_;
};

} // namespace capstone
