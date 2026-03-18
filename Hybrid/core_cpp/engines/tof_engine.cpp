#include "tof_engine.h"
#include "trilateration.h"
#include <numeric>
#include <algorithm>

namespace capstone {

ToFEngine::ToFEngine(int window_size, double max_dist_m,
                     double room_w, double room_h)
    : window_size_(window_size)
    , max_dist_m_(max_dist_m)
    , room_w_(room_w)
    , room_h_(room_h)
{}

void ToFEngine::set_anchors(const std::vector<AnchorDef>& anchors) {
    anchors_ = anchors;
}

void ToFEngine::set_rooms(const std::vector<RoomDef>& rooms) {
    rooms_ = rooms;
}

void ToFEngine::feed(const std::string& anchor_id, double distance_m) {
    // Hard outlier gate — discard implausible readings before they pollute history
    if (distance_m <= 0.0 || distance_m > max_dist_m_)
        return;

    auto& dq = history_[anchor_id];
    dq.push_back(distance_m);
    if (static_cast<int>(dq.size()) > window_size_)
        dq.pop_front();
}

bool ToFEngine::has_data(int min_anchors) const {
    int count = 0;
    for (const auto& a : anchors_) {
        if (history_.count(a.id) && !history_.at(a.id).empty())
            ++count;
    }
    return count >= min_anchors;
}

ToFEngine::ToFResult ToFEngine::solve(double clamp_margin, double max_dist_conf) const {
    // Build averaged distances for each anchor that has history
    std::vector<std::pair<double, double>> anchor_coords;
    std::vector<double>                    avg_dists;

    for (const auto& a : anchors_) {
        auto it = history_.find(a.id);
        if (it == history_.end() || it->second.empty())
            continue;

        const auto& dq = it->second;
        double avg = std::accumulate(dq.begin(), dq.end(), 0.0) / dq.size();
        anchor_coords.push_back({a.x, a.y});
        avg_dists.push_back(avg);
    }

    if (anchor_coords.size() < 2) {
        return {"Outside Defined Area", 0.0, room_w_ / 2.0, room_h_ / 2.0};
    }

    // Re-use the same trilateration solver as RSSIEngine
    auto [ex, ey] = refined_trilaterate(anchor_coords, avg_dists, room_w_, room_h_);

    // Classify room (clamping handled inside classify_position)
    auto cls = classify_position(ex, ey, rooms_,
                                 room_w_, room_h_,
                                 clamp_margin, max_dist_conf);
    return {cls.room_name, cls.confidence, cls.x, cls.y};
}

} // namespace capstone
