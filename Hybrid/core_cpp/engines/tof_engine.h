#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <deque>
#include "geometry.h"

namespace capstone {

/// Time-of-Flight localisation engine for ESP32-C3 BLE anchors.
///
/// Each anchor publishes a measured distance (metres) to the target.
/// This engine maintains a short rolling history per anchor (to smooth
/// noisy UWB/BLE ranging) and then runs the same bounded trilateration
/// solver used by RSSIEngine — the math is identical once distances are known.
///
/// Integration notes:
///   - MQTTPipe delivers ToF measurements one anchor at a time.
///   - Call feed() for each incoming measurement.
///   - Call solve()  when you want a position estimate (e.g. every update_interval).
class ToFEngine {
public:
    struct AnchorDef {
        std::string id;
        double x, y;
    };

    struct ToFResult {
        std::string room_name;
        double      confidence;
        double      x, y;
    };

    /// @param window_size    Rolling-average window per anchor.
    /// @param max_dist_m     Discard measurements above this (outlier gate).
    /// @param room_w / room_h  Floor plan dimensions for boundary clamping.
    ToFEngine(int    window_size  = 5,
              double max_dist_m   = 20.0,
              double room_w       = 10.0,
              double room_h       = 10.0);

    void set_anchors(const std::vector<AnchorDef>& anchors);
    void set_rooms(const std::vector<RoomDef>& rooms);

    /// Feed one distance measurement from a single anchor.
    /// Call this from the MQTT callback for every received ToF packet.
    void feed(const std::string& anchor_id, double distance_m);

    /// Solve position from the current rolling-average distances.
    /// Returns a result even if fewer than 3 anchors are visible
    /// (falls back to room centre when insufficient data).
    ///
    /// @param clamp_margin     Boundary clamp margin (metres).
    /// @param max_dist_conf    Distance to room centre for confidence 0.
    ToFResult solve(double clamp_margin  = 0.01,
                    double max_dist_conf = 3.0) const;

    /// True if at least `min_anchors` have data in their history.
    bool has_data(int min_anchors = 2) const;

private:
    int    window_size_;
    double max_dist_m_;
    double room_w_, room_h_;

    std::vector<AnchorDef>   anchors_;
    std::vector<RoomDef>     rooms_;

    // Rolling history per anchor id
    std::unordered_map<std::string, std::deque<double>> history_;
};

} // namespace capstone
