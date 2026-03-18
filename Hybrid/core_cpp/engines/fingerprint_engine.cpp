#include "fingerprint_engine.h"

namespace capstone {

FingerprintEngine::FingerprintEngine(int    window_size,
                                     double noise_floor_dbm,
                                     int    k,
                                     double confidence_baseline)
    : filter_(window_size, noise_floor_dbm)
    , k_(k)
    , confidence_baseline_(confidence_baseline)
{}

void FingerprintEngine::set_radio_map(const std::vector<RadioMapEntry>& radio_map) {
    radio_map_ = radio_map;
}

std::unordered_map<std::string, KNNResult>
FingerprintEngine::process_cycle(const RSSIFilter::RSSIMap& raw_rssi,
                                 const std::vector<std::string>& ssids)
{
    // 1. Smooth raw RSSI through the stateful rolling-average filter
    auto smoothed = filter_.process(raw_rssi);

    std::unordered_map<std::string, KNNResult> results;

    for (const auto& ssid : ssids) {
        // 2. Build a live RSSI vector for this device across all visible APs
        RSSIVector live_vec;
        for (const auto& [ap_id, dev_map] : smoothed) {
            auto it = dev_map.find(ssid);
            if (it != dev_map.end() && it->second != -999.0) {
                live_vec[ap_id] = it->second;
            }
        }

        // 3. KNN match against the radio map
        if (live_vec.empty() || radio_map_.empty()) {
            results[ssid] = {"Outside Defined Area", 0.0};
        } else {
            results[ssid] = knn_fingerprint_match(
                live_vec, radio_map_, k_, confidence_baseline_);
        }
    }

    return results;
}

} // namespace capstone
