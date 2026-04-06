#include "anchor_positioning.h"
#include "trilateration.h"

#include <cmath>
#include <numeric>
#include <algorithm>

namespace capstone {

// ─────────────────────────────────────────────────────────────────────────────
//  Construction
// ─────────────────────────────────────────────────────────────────────────────

AnchorPosEngine::AnchorPosEngine(int window_size, double max_dist_m,
                                 double room_w,   double room_h)
    : window_size_(window_size)
    , max_dist_m_(max_dist_m)
    , room_w_(room_w)
    , room_h_(room_h)
    , filter_(window_size, -95.0)     // -95 dBm noise floor for target scanning
    , ia_filter_(window_size, -95.0)  // same floor for inter-anchor observations
{}

// ─────────────────────────────────────────────────────────────────────────────
//  Setup
// ─────────────────────────────────────────────────────────────────────────────

void AnchorPosEngine::set_anchors(const std::vector<AnchorDef>& anchors) {
    std::lock_guard<std::mutex> lk(mu_);
    anchors_ = anchors;
    anchor_map_.clear();
    anchor_weights_.clear();
    for (const auto& a : anchors) {
        anchor_map_[a.id] = a;
        anchor_weights_[a.id] = 1.0;  // uniform trust until variance is assessed
    }
}

// ─────────────────────────────────────────────────────────────────────────────
//  RSSI ingestion
// ─────────────────────────────────────────────────────────────────────────────

void AnchorPosEngine::feed_rssi(const std::string& observer_ap_id,
                                const std::string& target_id,
                                double             rssi_dbm) {
    std::lock_guard<std::mutex> lk(mu_);
    double smoothed = filter_.feed(observer_ap_id, target_id, rssi_dbm);
    if (smoothed > -999.0) {
        // Cache the latest smoothed value so rssi_to_dist_result() can read
        // it without issuing another feed() call (which would add a junk sample).
        target_smoothed_[observer_ap_id][target_id] = smoothed;
    }
}

void AnchorPosEngine::feed_rssi_batch(const RSSIFilter::RSSIMap& batch) {
    // Process the entire {ap_id: {device_id: rssi}} map under a single lock.
    // This is the natural ingestion path when data arrives as a Python dict
    // from the MQTT handler — avoids per-reading lock/unlock overhead.
    std::lock_guard<std::mutex> lk(mu_);
    for (const auto& [ap_id, targets] : batch) {
        for (const auto& [target_id, rssi_dbm] : targets) {
            double smoothed = filter_.feed(ap_id, target_id, rssi_dbm);
            if (smoothed > -999.0) {
                target_smoothed_[ap_id][target_id] = smoothed;
            }
        }
    }
}

void AnchorPosEngine::feed_inter_anchor(const std::string& observer_id,
                                        const std::string& target_id,
                                        double             rssi_dbm) {
    std::lock_guard<std::mutex> lk(mu_);
    double smoothed = ia_filter_.feed(observer_id, target_id, rssi_dbm);
    if (smoothed > -999.0) {
        // Cache smoothed inter-anchor reading for P0 calibration and
        // variance detection (both are read-only, no additional feed allowed).
        ia_smoothed_[observer_id][target_id] = smoothed;
    }
}

void AnchorPosEngine::feed_inter_anchor_batch(const RSSIFilter::RSSIMap& batch) {
    std::lock_guard<std::mutex> lk(mu_);
    for (const auto& [observer_id, targets] : batch) {
        for (const auto& [target_id, rssi_dbm] : targets) {
            double smoothed = ia_filter_.feed(observer_id, target_id, rssi_dbm);
            if (smoothed > -999.0) {
                ia_smoothed_[observer_id][target_id] = smoothed;
            }
        }
    }
}

// ─────────────────────────────────────────────────────────────────────────────
//  P0 calibration
// ─────────────────────────────────────────────────────────────────────────────

double AnchorPosEngine::get_rssi_at_1m(const std::string& anchor_id) const {
    // NOTE: caller must hold mu_ or call via the public const path (no lock here
    //       because this method is called from within other locked methods).

    auto it = anchor_map_.find(anchor_id);
    if (it == anchor_map_.end()) {
        return -60.0;  // safe indoor default
    }
    const AnchorDef& obs_anchor = it->second;

    // Collect P0 estimates from every other anchor that obs_anchor has observed.
    //
    // For a pair (observer = anchor_id, target = peer_id):
    //   path-loss model:  RSSI(d) = P0 - 10 * n * log10(d)
    //   rearranged:       P0      = RSSI(d) + 10 * n * log10(d)
    //
    // d is the known Euclidean distance between the two anchors (ground truth).
    // n and the fallback P0 come from the *observer* anchor's AnchorDef because
    // P0 here represents the observer's receiver sensitivity in its environment.
    std::vector<double> p0_estimates;

    auto obs_cache_it = ia_smoothed_.find(anchor_id);
    if (obs_cache_it != ia_smoothed_.end()) {
        for (const auto& [peer_id, smoothed_rssi] : obs_cache_it->second) {
            auto peer_it = anchor_map_.find(peer_id);
            if (peer_it == anchor_map_.end()) continue;
            const AnchorDef& peer = peer_it->second;

            double dx         = obs_anchor.x - peer.x;
            double dy         = obs_anchor.y - peer.y;
            double true_dist  = std::sqrt(dx * dx + dy * dy);

            if (true_dist < 0.1) continue;  // co-located anchors — skip

            double p0 = smoothed_rssi + 10.0 * obs_anchor.path_loss_n
                                             * std::log10(true_dist);
            p0_estimates.push_back(p0);
        }
    }

    if (p0_estimates.empty()) {
        // No inter-anchor observations yet — return the configured fallback.
        return obs_anchor.rssi_at_1m;
    }

    double sum = std::accumulate(p0_estimates.begin(), p0_estimates.end(), 0.0);
    return sum / static_cast<double>(p0_estimates.size());
}

std::unordered_map<std::string, double> AnchorPosEngine::get_all_p0() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::unordered_map<std::string, double> result;
    for (const auto& [id, _] : anchor_map_) {
        result[id] = get_rssi_at_1m(id);
    }
    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Private: RSSI → distance conversion
// ─────────────────────────────────────────────────────────────────────────────

std::vector<AnchorPosEngine::AnchorDist>
AnchorPosEngine::rssi_to_dist_result(const std::string& target_id) const {
    // NOTE: caller holds mu_.

    std::vector<AnchorDist> result;
    result.reserve(anchor_map_.size());

    for (const auto& [ap_id, anchor_def] : anchor_map_) {
        // 1. Look up the cached smoothed RSSI from this AP for target_id.
        auto ap_it = target_smoothed_.find(ap_id);
        if (ap_it == target_smoothed_.end()) continue;
        auto tgt_it = ap_it->second.find(target_id);
        if (tgt_it == ap_it->second.end()) continue;

        double smoothed_rssi = tgt_it->second;

        // 2. Use live-calibrated P0 for this observer anchor; fall back to the
        //    configured value if inter-anchor data is not yet available.
        double p0   = get_rssi_at_1m(ap_id);
        double dist = rssi_to_distance_m(smoothed_rssi, p0, anchor_def.path_loss_n);

        // 3. Range gate: discard implausibly far estimates.
        if (dist <= 0.0 || dist > max_dist_m_) continue;

        result.push_back({ap_id, anchor_def.x, anchor_def.y, dist});
    }

    return result;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Private: trilateration solve
// ─────────────────────────────────────────────────────────────────────────────

AnchorPosResult AnchorPosEngine::trilateration_solver(
    const std::vector<AnchorDist>& candidates) const
{
    // NOTE: caller holds mu_.

    AnchorPosResult out;
    out.n_anchors = static_cast<int>(candidates.size());

    if (candidates.size() < 2) {
        // Not enough data yet — return room centre as a neutral fallback.
        out.x          = room_w_ / 2.0;
        out.y          = room_h_ / 2.0;
        out.confidence = 0.0;
        return out;
    }

    // Build anchor + distance vectors for refined_trilaterate.
    // Per-anchor weights (from variance detection) are applied by scaling the
    // distance estimate: a less-trusted anchor's distance is nudged slightly
    // toward the fallback value so the gradient-descent solver deprioritises it.
    //
    // When all weights are 1.0 (current stub state) this is a no-op and
    // we get the same result as calling refined_trilaterate directly.
    std::vector<std::pair<double, double>> anchors;
    std::vector<double>                    dists;
    anchors.reserve(candidates.size());
    dists.reserve(candidates.size());

    for (const auto& c : candidates) {
        double weight = 1.0;
        auto   w_it   = anchor_weights_.find(c.id);
        if (w_it != anchor_weights_.end()) weight = w_it->second;

        // Weight ∈ [0,1]: low weight → blend distance toward max_dist_m_
        // (a high-distance value pulls the solver away from that anchor).
        // TODO: replace this simple blend with proper weighted least-squares
        //       once rssi_variance_detection() is fully implemented.
        double blended_dist = c.dist_m * weight + max_dist_m_ * (1.0 - weight);

        anchors.push_back({c.x, c.y});
        dists.push_back(blended_dist);
    }

    auto [ex, ey] = refined_trilaterate(anchors, dists, room_w_, room_h_);

    // Confidence heuristic: saturates at 1.0 with 4+ good anchors.
    out.x          = ex;
    out.y          = ey;
    out.confidence = std::min(1.0, static_cast<double>(candidates.size()) / 4.0);

    return out;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Public: get position
// ─────────────────────────────────────────────────────────────────────────────

AnchorPosResult AnchorPosEngine::get_position(const std::string& target_id) {
    std::lock_guard<std::mutex> lk(mu_);
    auto candidates = rssi_to_dist_result(target_id);
    auto result     = trilateration_solver(candidates);
    last_result_[target_id] = result;
    return result;
}

std::unordered_map<std::string, AnchorPosResult>
AnchorPosEngine::get_all_positions() {
    // Recompute positions for every target that has at least one valid smoothed
    // RSSI reading.  This is the primary output path called by the FastAPI
    // polling endpoint — one call covers all tracked devices.
    std::lock_guard<std::mutex> lk(mu_);
    std::unordered_map<std::string, AnchorPosResult> out;

    // Refresh per-anchor confidence weights from inter-anchor RSSI geometry
    // before trilateration so every position estimate in this batch uses
    // up-to-date weights without needing a separate rssi_variance_detection() call.
    update_anchor_weights_locked();

    // Collect the set of all target IDs seen across all AP caches.
    std::unordered_map<std::string, bool> target_set;
    for (const auto& [ap_id, targets] : target_smoothed_) {
        for (const auto& [target_id, _] : targets) {
            target_set[target_id] = true;
        }
    }

    for (const auto& [target_id, _] : target_set) {
        auto candidates         = rssi_to_dist_result(target_id);
        auto result             = trilateration_solver(candidates);
        last_result_[target_id] = result;
        out[target_id]          = result;
    }
    return out;
}

AnchorPosResult AnchorPosEngine::get_last_result(const std::string& target_id) const {
    std::lock_guard<std::mutex> lk(mu_);
    auto it = last_result_.find(target_id);
    if (it == last_result_.end()) {
        return AnchorPosResult{};  // zeroed: x=0, y=0, confidence=0, n_anchors=0
    }
    return it->second;
}

std::vector<std::string> AnchorPosEngine::get_known_targets() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::vector<std::string> targets;
    for (const auto& [ap_id, device_map] : target_smoothed_) {
        for (const auto& [target_id, _] : device_map) {
            // Deduplicate: only add if not already present.
            bool found = false;
            for (const auto& t : targets) {
                if (t == target_id) { found = true; break; }
            }
            if (!found) targets.push_back(target_id);
        }
    }
    std::sort(targets.begin(), targets.end());
    return targets;
}

// ─────────────────────────────────────────────────────────────────────────────
//  Variance detection  (STUB — implementation deferred)
// ─────────────────────────────────────────────────────────────────────────────

// Private — caller MUST hold mu_.
void AnchorPosEngine::update_anchor_weights_locked() {
    // For each inter-anchor observation (observer A sees target B):
    //
    //   measured_dist = rssi_to_distance_m(ia_smoothed[A][B], P0_B, n_B)
    //     → P0 and n come from B's AnchorDef (its tx characteristics), not A's.
    //       This controls for B's hardware so residual error reflects A's read quality.
    //
    //   true_dist = ||A.pos - B.pos||₂  (from configured geometry)
    //
    //   error_m = |measured_dist - true_dist|
    //
    //   weight  = 1 / (1 + error_m²)
    //     → Inverse-square decay: 0 m error → 1.0,  1 m error → 0.5,  3 m error → 0.1
    //
    //   anchor_weights_[A] = mean(weight) across all peers B that A has observed.
    //
    // Anchors with no inter-anchor data retain their existing weight (1.0 on init).

    std::unordered_map<std::string, std::vector<double>> samples;
    for (const auto& [id, _] : anchor_map_) {
        samples[id] = {};
    }

    for (const auto& [observer_id, targets] : ia_smoothed_) {
        auto obs_it = anchor_map_.find(observer_id);
        if (obs_it == anchor_map_.end()) continue;
        const AnchorDef& obs = obs_it->second;

        for (const auto& [target_id, smoothed_rssi] : targets) {
            auto tgt_it = anchor_map_.find(target_id);
            if (tgt_it == anchor_map_.end()) continue;
            const AnchorDef& tgt = tgt_it->second;

            double dx        = obs.x - tgt.x;
            double dy        = obs.y - tgt.y;
            double true_dist = std::sqrt(dx * dx + dy * dy);
            if (true_dist < 0.1) continue;   // co-located

            // Use target's static rssi_at_1m to avoid circular dependency with
            // get_rssi_at_1m() (which reads ia_smoothed_ itself).
            double measured = rssi_to_distance_m(
                smoothed_rssi, tgt.rssi_at_1m, tgt.path_loss_n);
            if (measured <= 0.0) continue;

            double error_m = std::abs(measured - true_dist);
            double w       = 1.0 / (1.0 + error_m * error_m);
            samples[observer_id].push_back(w);
        }
    }

    for (const auto& [anchor_id, w_vec] : samples) {
        if (w_vec.empty()) continue;
        double sum  = std::accumulate(w_vec.begin(), w_vec.end(), 0.0);
        anchor_weights_[anchor_id] = sum / static_cast<double>(w_vec.size());
    }
}

std::unordered_map<std::string, double> AnchorPosEngine::rssi_variance_detection() {
    std::lock_guard<std::mutex> lk(mu_);
    update_anchor_weights_locked();
    return anchor_weights_;
}

std::unordered_map<std::string, double> AnchorPosEngine::get_anchor_weights() const {
    std::lock_guard<std::mutex> lk(mu_);
    return anchor_weights_;
}

RSSIFilter::RSSIMap AnchorPosEngine::get_target_rssi_cache() const {
    std::lock_guard<std::mutex> lk(mu_);
    return target_smoothed_;
}

// ─────────────────────────────────────────────────────────────────────────────
//  State management
// ─────────────────────────────────────────────────────────────────────────────

void AnchorPosEngine::clear_target(const std::string& target_id) {
    std::lock_guard<std::mutex> lk(mu_);
    // Remove this target from every AP's smoothed cache.
    for (auto& [ap_id, device_map] : target_smoothed_) {
        device_map.erase(target_id);
    }
    // Remove the filter history for this target across all APs.
    // RSSIFilter stores history as history_[ap_id][device_id]; we can't reach
    // into it directly, but clearing the smoothed cache is enough to stop
    // this target contributing to future get_position() calls.
    // The filter's stale deque will be overwritten on the next feed_rssi() call.
    last_result_.erase(target_id);
}

void AnchorPosEngine::clear_all_targets() {
    std::lock_guard<std::mutex> lk(mu_);
    target_smoothed_.clear();
    last_result_.clear();
    // Reset the target-scanning filter completely; inter-anchor data is preserved
    // so P0 calibration and variance weights remain valid.
    filter_.clear();
}

// ─────────────────────────────────────────────────────────────────────────────
//  Diagnostics
// ─────────────────────────────────────────────────────────────────────────────

std::unordered_map<std::string, std::pair<double, double>>
AnchorPosEngine::create_anchor_map() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::unordered_map<std::string, std::pair<double, double>> out;
    for (const auto& a : anchors_) {
        out[a.id] = {a.x, a.y};
    }
    return out;
}

} // namespace capstone
