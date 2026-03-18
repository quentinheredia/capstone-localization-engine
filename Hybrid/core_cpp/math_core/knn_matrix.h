#pragma once

#include <vector>
#include <string>
#include <unordered_map>
#include <utility>

namespace capstone {

/// A single fingerprint vector: AP_ID -> RSSI (dBm).
using RSSIVector = std::unordered_map<std::string, double>;

/// One entry in the radio map: a room label + its calibration vectors.
struct RadioMapEntry {
    std::string room;
    std::vector<RSSIVector> vectors;
};

/// KNN match result.
struct KNNResult {
    std::string room;
    double confidence;
};

/// Compute Euclidean distance between two RSSI vectors.
/// Only APs present in BOTH vectors contribute.
/// Returns {distance, num_common_aps}.
std::pair<double, int> euclidean_rssi_distance(const RSSIVector& a,
                                                const RSSIVector& b);

/// Run K-Nearest-Neighbours against the radio map.
/// Returns best room + confidence (1.0 - avg_dist / baseline).
KNNResult knn_fingerprint_match(
    const RSSIVector& live_vector,
    const std::vector<RadioMapEntry>& radio_map,
    int k = 3,
    double confidence_baseline = 30.0);

} // namespace capstone
