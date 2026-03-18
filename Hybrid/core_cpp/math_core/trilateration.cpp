#include "trilateration.h"
#include <cmath>
#include <algorithm>
#include <numeric>

namespace capstone {

double rssi_to_distance_m(double rssi_dbm, double p0_dbm, double n) {
    double d = std::pow(10.0, (p0_dbm - rssi_dbm) / (10.0 * n));
    return std::max(std::min(d, 50.0), 0.05);
}

std::pair<double, double> bounded_trilaterate(
    const std::vector<std::pair<double, double>>& anchors,
    const std::vector<double>& dists,
    double room_w, double room_h,
    double init_x, double init_y)
{
    double cx = init_x;
    double cy = init_y;

    const double lr  = 0.1;
    const int    max_iter = 200;
    const double eps = 1e-6;

    for (int i = 0; i < max_iter; ++i) {
        double gx = 0.0, gy = 0.0;

        for (size_t j = 0; j < anchors.size(); ++j) {
            double dx = cx - anchors[j].first;
            double dy = cy - anchors[j].second;
            double d_calc = std::sqrt(dx * dx + dy * dy);
            if (d_calc < 1e-4) d_calc = 1e-4;

            double diff = d_calc - dists[j];
            gx += 2.0 * diff * (dx / d_calc);
            gy += 2.0 * diff * (dy / d_calc);
        }

        double nx = cx - lr * gx;
        double ny = cy - lr * gy;

        // Clamp to room bounds
        nx = std::max(0.0, std::min(room_w, nx));
        ny = std::max(0.0, std::min(room_h, ny));

        if (std::abs(nx - cx) < eps && std::abs(ny - cy) < eps)
            break;

        cx = nx;
        cy = ny;
    }
    return {cx, cy};
}

std::pair<double, double> refined_trilaterate(
    const std::vector<std::pair<double, double>>& anchors,
    const std::vector<double>& dists,
    double room_w, double room_h)
{
    std::vector<std::pair<double, double>> goodA;
    std::vector<double> goodD;

    for (size_t i = 0; i < anchors.size(); ++i) {
        if (dists[i] >= 0.05 && dists[i] <= 50.0) {
            goodA.push_back(anchors[i]);
            goodD.push_back(dists[i]);
        }
    }

    if (goodA.size() < 2)
        return {room_w / 2.0, room_h / 2.0};

    // Weighted centroid initial guess
    double x0 = 0.0, y0 = 0.0, tw = 0.0;
    for (size_t i = 0; i < goodA.size(); ++i) {
        double w = 1.0 / (goodD[i] * goodD[i]);
        x0 += goodA[i].first  * w;
        y0 += goodA[i].second * w;
        tw += w;
    }
    if (tw > 0.0) { x0 /= tw; y0 /= tw; }

    return bounded_trilaterate(goodA, goodD, room_w, room_h, x0, y0);
}

} // namespace capstone
