#pragma once

#include <vector>
#include <utility>
#include <string>

namespace capstone {

/// Ray-casting point-in-polygon test.
/// polygon: ordered list of (x,y) vertices.
bool point_in_polygon(double px, double py,
                      const std::vector<std::pair<double, double>>& polygon);

/// Clamp (x, y) to within [margin, dim - margin].
/// Returns {clamped_x, clamped_y, was_clamped}.
struct ClampResult {
    double x;
    double y;
    bool   clamped;
};

ClampResult boundary_clamp(double x, double y,
                           double room_w, double room_h,
                           double margin);

/// Full room classification: clamp → polygon check → confidence.
/// rooms: vector of {room_name, center_x, center_y, polygon}.
struct RoomClassification {
    std::string room_name;
    double confidence;
    double x;
    double y;
};

struct RoomDef {
    std::string name;
    double center_x;
    double center_y;
    std::vector<std::pair<double, double>> polygon;
};

RoomClassification classify_position(
    double est_x, double est_y,
    const std::vector<RoomDef>& rooms,
    double room_w, double room_h,
    double clamp_margin,
    double max_dist_high_conf);

} // namespace capstone
