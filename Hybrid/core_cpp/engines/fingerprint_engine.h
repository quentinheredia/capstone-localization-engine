#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include "knn_matrix.h"
#include "signal_filters.h"

namespace capstone {

/// Stateful KNN fingerprinting engine.
///
/// Mirrors RSSIEngine for the fingerprinting localisation path.
/// Owns the RSSIFilter (rolling average) so smoothed vectors are passed to
/// the KNN matcher — matching raw noisy RSSI directly degrades accuracy.
///
/// Python side loads radiomap.json and converts it to vector<RadioMapEntry>
/// before constructing this engine.  The engine is then called per cycle.
class FingerprintEngine {
public:
    /// @param window_size      RSSIFilter rolling-average window.
    /// @param noise_floor_dbm  Readings below this are discarded.
    /// @param k                Number of nearest neighbours for voting.
    /// @param confidence_baseline  Distance at which confidence reaches 0.
    explicit FingerprintEngine(int    window_size       = 5,
                               double noise_floor_dbm   = -80.0,
                               int    k                 = 3,
                               double confidence_baseline = 30.0);

    /// Replace the in-memory radio map (call when radiomap.json is reloaded).
    void set_radio_map(const std::vector<RadioMapEntry>& radio_map);

    /// Process one raw RSSI map, smooth it, then KNN-match each target SSID.
    ///
    /// @param raw_rssi  {ap_id: {ssid: rssi_dbm}}
    /// @param ssids     List of target SSIDs to locate.
    /// @returns         {ssid: KNNResult}
    std::unordered_map<std::string, KNNResult>
    process_cycle(const RSSIFilter::RSSIMap& raw_rssi,
                  const std::vector<std::string>& ssids);

private:
    RSSIFilter                 filter_;
    std::vector<RadioMapEntry> radio_map_;
    int    k_;
    double confidence_baseline_;
};

} // namespace capstone
