#include "rssi_engine.h"
#include "trilateration.h"

namespace capstone {

RSSIEngine::RSSIEngine(int window_size, double noise_floor_dbm,
                       int min_aps, double clamp_margin,
                       double max_dist_conf,
                       double room_w, double room_h)
    : filter_(window_size, noise_floor_dbm)
    , min_aps_(min_aps)
    , clamp_margin_(clamp_margin)
    , max_dist_conf_(max_dist_conf)
    , room_w_(room_w)
    , room_h_(room_h)
{}

void RSSIEngine::set_aps(const std::vector<APDef>& aps) {
    aps_ = aps;
}

void RSSIEngine::set_rooms(const std::vector<RoomDef>& rooms) {
    rooms_ = rooms;
}

std::vector<RSSIEngine::LocalizationResult> RSSIEngine::process_cycle(
    const RSSIFilter::RSSIMap& raw_rssi,
    const std::vector<TargetDef>& targets)
{
    // 1. Smooth the raw RSSI through the stateful filter
    auto smoothed = filter_.process(raw_rssi);

    std::vector<LocalizationResult> results;
    results.reserve(targets.size());

    for (const auto& tgt : targets) {
        LocalizationResult lr;
        lr.device_id  = tgt.ssid;
        lr.room       = "Undetected";
        lr.confidence = 0.0;
        lr.x = 0.0;
        lr.y = 0.0;

        // 2. Build the live RSSI vector for this target
        std::vector<std::pair<double, double>> anchors;
        std::vector<double> dists;

        for (const auto& ap : aps_) {
            auto ap_it = smoothed.find(ap.id);
            if (ap_it == smoothed.end()) continue;

            auto dev_it = ap_it->second.find(tgt.ssid);
            if (dev_it == ap_it->second.end()) continue;

            double rssi = dev_it->second;
            double dist = rssi_to_distance_m(rssi, tgt.rssi_at_1m, tgt.path_loss_n);
            anchors.push_back({ap.x, ap.y});
            dists.push_back(dist);
        }

        // 3. Trilaterate if enough APs
        if (static_cast<int>(anchors.size()) >= min_aps_) {
            auto [ex, ey] = refined_trilaterate(anchors, dists, room_w_, room_h_);

            // 4. Classify room
            auto cls = classify_position(ex, ey, rooms_,
                                         room_w_, room_h_,
                                         clamp_margin_, max_dist_conf_);
            lr.room       = cls.room_name;
            lr.confidence = cls.confidence;
            lr.x          = cls.x;
            lr.y          = cls.y;
        }

        results.push_back(std::move(lr));
    }

    return results;
}

} // namespace capstone
