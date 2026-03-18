#include "signal_filters.h"
#include <numeric>

namespace capstone {

RSSIFilter::RSSIFilter(int window_size, double noise_floor_dbm)
    : window_size_(window_size)
    , noise_floor_dbm_(noise_floor_dbm)
{}

double RSSIFilter::feed(const std::string& ap_id,
                        const std::string& device_id,
                        double raw_rssi) {
    // Noise floor gate
    if (raw_rssi < noise_floor_dbm_)
        return -999.0;

    // Dynamic initialization (matches OG dynamic deque creation)
    auto& dq = history_[ap_id][device_id];

    dq.push_back(raw_rssi);

    // Enforce window size
    while (static_cast<int>(dq.size()) > window_size_)
        dq.pop_front();

    // Rolling mean
    double sum = 0.0;
    for (double v : dq) sum += v;
    return sum / static_cast<double>(dq.size());
}

RSSIFilter::RSSIMap RSSIFilter::process(const RSSIMap& raw) {
    RSSIMap out;

    for (const auto& [ap_id, devices] : raw) {
        for (const auto& [device_id, rssi] : devices) {
            double smoothed = feed(ap_id, device_id, rssi);
            if (smoothed > -999.0) {
                out[ap_id][device_id] = smoothed;
            }
        }
    }
    return out;
}

void RSSIFilter::clear() {
    history_.clear();
}

} // namespace capstone
