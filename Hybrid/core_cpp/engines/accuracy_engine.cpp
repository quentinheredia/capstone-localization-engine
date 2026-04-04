/**
 * accuracy_engine.cpp
 *
 * Implementation of AccuracyEngine.
 * See accuracy_engine.h for design notes.
 */

#include "accuracy_engine.h"
#include <stdexcept>
#include <algorithm>

namespace capstone {

// ─────────────────────────────────────────────────────────────────────────────
AccuracyEngine::AccuracyEngine(int max_buf) : max_buf_(max_buf) {
    if (max_buf_ < 1)
        throw std::invalid_argument("AccuracyEngine: max_buf must be >= 1");
}

// ── Ground truth ──────────────────────────────────────────────────────────────

void AccuracyEngine::set_ground_truth(double x, double y) {
    std::lock_guard<std::mutex> lk(mu_);
    gt_x_ = x;
    gt_y_ = y;
}

std::pair<double, double> AccuracyEngine::get_ground_truth() const {
    std::lock_guard<std::mutex> lk(mu_);
    return { gt_x_, gt_y_ };
}

// ── Data ingestion ────────────────────────────────────────────────────────────

void AccuracyEngine::push_estimate(const std::string& method,
                                   double est_x, double est_y,
                                   const std::string& scenario,
                                   int64_t ts_ms)
{
    if (method.empty())
        throw std::invalid_argument("AccuracyEngine::push_estimate: method key must not be empty");

    std::lock_guard<std::mutex> lk(mu_);

    // Euclidean error against current ground truth
    const double dx = est_x - gt_x_;
    const double dy = est_y - gt_y_;
    const double raw_err = std::sqrt(dx * dx + dy * dy);

    const bool   is_out  = raw_err > ACCURACY_HARD_CAP_M;
    const double capped  = is_out ? ACCURACY_HARD_CAP_M : raw_err;

    ErrorSample s;
    s.timestamp_ms = (ts_ms > 0) ? ts_ms : _now_ms();
    s.error_m      = capped;
    s.raw_error_m  = raw_err;
    s.est_x        = est_x;
    s.est_y        = est_y;
    s.scenario     = scenario;
    s.is_outlier   = is_out;

    auto& dq = buffers_[method];
    dq.push_back(std::move(s));
    // Rolling eviction — keep the newest max_buf_ samples
    while (static_cast<int>(dq.size()) > max_buf_)
        dq.pop_front();
}

// ── Data retrieval ────────────────────────────────────────────────────────────

std::vector<ErrorSample> AccuracyEngine::get_buffer(const std::string& method) const {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = buffers_.find(method);
    if (it == buffers_.end())
        return {};
    return std::vector<ErrorSample>(it->second.begin(), it->second.end());
}

std::vector<std::string> AccuracyEngine::method_keys() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::vector<std::string> keys;
    keys.reserve(buffers_.size());
    for (const auto& kv : buffers_)
        if (!kv.second.empty())
            keys.push_back(kv.first);
    std::sort(keys.begin(), keys.end());
    return keys;
}

void AccuracyEngine::clear_method(const std::string& method) {
    std::lock_guard<std::mutex> lk(mu_);
    buffers_.erase(method);
}

void AccuracyEngine::clear_all() {
    std::lock_guard<std::mutex> lk(mu_);
    buffers_.clear();
}

} // namespace capstone
