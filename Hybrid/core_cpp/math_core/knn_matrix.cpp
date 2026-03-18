#include "knn_matrix.h"
#include <cmath>
#include <algorithm>
#include <unordered_map>

namespace capstone {

std::pair<double, int> euclidean_rssi_distance(const RSSIVector& a,
                                                const RSSIVector& b) {
    double sq_sum = 0.0;
    int common = 0;

    for (const auto& [ap, rssi_a] : a) {
        auto it = b.find(ap);
        if (it != b.end()) {
            double diff = rssi_a - it->second;
            sq_sum += diff * diff;
            ++common;
        }
    }
    return {std::sqrt(sq_sum), common};
}

KNNResult knn_fingerprint_match(
    const RSSIVector& live_vector,
    const std::vector<RadioMapEntry>& radio_map,
    int k,
    double confidence_baseline)
{
    struct DistRoom {
        double dist;
        std::string room;
    };
    std::vector<DistRoom> candidates;

    for (const auto& entry : radio_map) {
        for (const auto& map_vec : entry.vectors) {
            auto [dist, common] = euclidean_rssi_distance(live_vector, map_vec);
            if (common > 0) {
                candidates.push_back({dist, entry.room});
            }
        }
    }

    if (candidates.empty())
        return {"Outside Defined Area", 0.0};

    std::sort(candidates.begin(), candidates.end(),
              [](const DistRoom& a, const DistRoom& b) { return a.dist < b.dist; });

    int top = std::min(k, static_cast<int>(candidates.size()));
    std::unordered_map<std::string, int> votes;
    double sum_dist = 0.0;

    for (int i = 0; i < top; ++i) {
        votes[candidates[i].room]++;
        sum_dist += candidates[i].dist;
    }

    std::string best_room;
    int best_count = 0;
    for (const auto& [room, count] : votes) {
        if (count > best_count) {
            best_count = count;
            best_room  = room;
        }
    }

    double avg_dist = sum_dist / top;
    double confidence = std::max(0.0, 1.0 - avg_dist / confidence_baseline);

    return {best_room, confidence};
}

} // namespace capstone
