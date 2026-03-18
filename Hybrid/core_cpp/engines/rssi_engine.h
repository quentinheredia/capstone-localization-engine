#pragma once

#include <string>
#include <vector>
#include <utility>
#include <unordered_map>
#include "signal_filters.h"
#include "geometry.h"
#include "knn_matrix.h"

namespace capstone {

/// High-level RSSI localization engine.
/// Wraps the filter + trilateration + room classification pipeline.
class RSSIEngine {
public:
    struct APDef {
        std::string id;
        double x, y;
    };

    struct TargetDef {
        std::string ssid;
        double rssi_at_1m;
        double path_loss_n;
    };

    struct LocalizationResult {
        std::string device_id;
        std::string room;
        double confidence;
        double x, y;
    };

    RSSIEngine(int window_size, double noise_floor_dbm,
               int min_aps, double clamp_margin,
               double max_dist_conf,
               double room_w, double room_h);

    /// Set the AP layout for the current floor.
    void set_aps(const std::vector<APDef>& aps);

    /// Set the room definitions for the current floor.
    void set_rooms(const std::vector<RoomDef>& rooms);

    /// Process one scan cycle.
    /// raw_rssi: {ap_id: {ssid: rssi_dbm}}.
    /// targets: list of target profiles.
    /// Returns one result per target.
    std::vector<LocalizationResult> process_cycle(
        const RSSIFilter::RSSIMap& raw_rssi,
        const std::vector<TargetDef>& targets);

private:
    RSSIFilter            filter_;
    std::vector<APDef>    aps_;
    std::vector<RoomDef>  rooms_;
    int    min_aps_;
    double clamp_margin_;
    double max_dist_conf_;
    double room_w_, room_h_;
};

} // namespace capstone
