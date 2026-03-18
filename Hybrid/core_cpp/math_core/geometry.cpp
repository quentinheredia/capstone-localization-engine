#include "geometry.h"
#include <cmath>
#include <algorithm>

namespace capstone {

bool point_in_polygon(double px, double py,
                      const std::vector<std::pair<double, double>>& polygon) {
    // Ray-casting algorithm (direct port of OG models.py Room.point_in_room)
    const int n = static_cast<int>(polygon.size());
    if (n < 3) return false;

    bool inside = false;
    double p1x = polygon[0].first;
    double p1y = polygon[0].second;

    for (int i = 1; i <= n; ++i) {
        double p2x = polygon[i % n].first;
        double p2y = polygon[i % n].second;

        if (py > std::min(p1y, p2y) && py <= std::max(p1y, p2y)) {
            if (px <= std::max(p1x, p2x)) {
                double xints = 0.0;
                if (p1y != p2y) {
                    xints = (py - p1y) * (p2x - p1x) / (p2y - p1y) + p1x;
                }
                if (p1x == p2x || px <= xints) {
                    inside = !inside;
                }
            }
        }
        p1x = p2x;
        p1y = p2y;
    }
    return inside;
}

ClampResult boundary_clamp(double x, double y,
                           double room_w, double room_h,
                           double margin) {
    bool clamped = false;

    if (x <= margin)            { x = margin;            clamped = true; }
    if (x >= room_w - margin)   { x = room_w - margin;   clamped = true; }
    if (y <= margin)            { y = margin;            clamped = true; }
    if (y >= room_h - margin)   { y = room_h - margin;   clamped = true; }

    return {x, y, clamped};
}

RoomClassification classify_position(
    double est_x, double est_y,
    const std::vector<RoomDef>& rooms,
    double room_w, double room_h,
    double clamp_margin,
    double max_dist_high_conf)
{
    auto cr = boundary_clamp(est_x, est_y, room_w, room_h, clamp_margin);

    RoomClassification result;
    result.x = cr.x;
    result.y = cr.y;
    result.room_name  = "Outside Defined Area";
    result.confidence = 0.0;

    // If clamped, bypass polygon check (matches OG is_clamped logic)
    if (cr.clamped)
        return result;

    for (const auto& room : rooms) {
        if (point_in_polygon(cr.x, cr.y, room.polygon)) {
            result.room_name = room.name;
            double dx = cr.x - room.center_x;
            double dy = cr.y - room.center_y;
            double dist = std::sqrt(dx * dx + dy * dy);
            result.confidence = std::max(0.0, 1.0 - dist / max_dist_high_conf);
            break;
        }
    }
    return result;
}

} // namespace capstone
