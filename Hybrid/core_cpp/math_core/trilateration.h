#pragma once

#include <vector>
#include <utility>

namespace capstone {

/// Convert RSSI (dBm) to distance (meters) via free-space path-loss model.
/// Clamped to [0.05, 50.0] metres.
double rssi_to_distance_m(double rssi_dbm, double p0_dbm, double n);

/// Raw bounded trilateration using gradient descent.
/// Returns (x, y) clamped within [0, room_w] x [0, room_h].
std::pair<double, double> bounded_trilaterate(
    const std::vector<std::pair<double, double>>& anchors,
    const std::vector<double>& dists,
    double room_w, double room_h,
    double init_x, double init_y);

/// Sanitizes inputs (filters 0.05–50 m), computes weighted centroid guess,
/// then delegates to bounded_trilaterate.  Falls back to room centre if < 2 good APs.
std::pair<double, double> refined_trilaterate(
    const std::vector<std::pair<double, double>>& anchors,
    const std::vector<double>& dists,
    double room_w, double room_h);

} // namespace capstone
