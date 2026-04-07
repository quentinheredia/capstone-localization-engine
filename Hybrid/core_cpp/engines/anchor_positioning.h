#pragma once

#include <string>
#include <vector>
#include <unordered_map>
#include <utility>
#include <mutex>

#include "signal_filters.h"   // also provides RSSIFilter::RSSIMap typedef

namespace capstone {

// ─────────────────────────────────────────────────────────────────────────────
//  Data types
// ─────────────────────────────────────────────────────────────────────────────

/// A fixed-position reference node (AP/anchor) in the room.
/// All fields have sensible defaults so Python can construct via keyword args
/// without setting every field explicitly.
struct AnchorDef {
    std::string id          = "";
    double      x           = 0.0;
    double      y           = 0.0;
    double      rssi_at_1m  = -60.0;  // P0: fallback reference RSSI at 1 m (dBm).
                                       // Overridden by live inter-anchor calibration
                                       // once enough observations are available.
    double      path_loss_n = 2.5;    // Path-loss exponent (indoors typically 2.0–3.5).
};

/// Position estimate produced by AnchorPosEngine for one target device.
struct AnchorPosResult {
    double x          = 0.0;
    double y          = 0.0;
    double confidence = 0.0;  // Heuristic 0..1 based on anchor count + weights.
    int    n_anchors  = 0;    // Number of anchors that contributed to this solve.
};


// ─────────────────────────────────────────────────────────────────────────────
//  AnchorPosEngine
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Anchor-assisted positioning engine.
 *
 * Design overview
 * ---------------
 * 1. Target scanning  — every anchor that sees a target device feeds its raw
 *    RSSI via feed_rssi().  The engine smooths these with per-AP rolling
 *    windows and converts them to distance estimates using the path-loss model.
 *
 * 2. Live P0 calibration — because anchors are at known, fixed positions they
 *    can observe *each other*.  Feed those readings via feed_inter_anchor().
 *    The engine back-calculates the receiver-side reference RSSI (P0) for each
 *    observer anchor from the known inter-anchor geometry, improving distance
 *    accuracy without any manual calibration walk.
 *
 * 3. Trilateration — get_position() runs refined_trilaterate() with the
 *    current distance set, weighted by per-anchor confidence.
 *
 * 4. Variance detection (stub) — rssi_variance_detection() will compare
 *    measured vs expected inter-anchor distances to produce per-anchor
 *    confidence weights.  Currently returns uniform weight 1.0.
 *
 * Thread safety
 * -------------
 * All public methods are guarded by an internal mutex.  feed_rssi(),
 * feed_inter_anchor(), and get_position() are safe to call concurrently.
 */
class AnchorPosEngine {
public:
    AnchorPosEngine(int window_size, double max_dist_m,
                    double room_w,   double room_h);

    // ── Setup ─────────────────────────────────────────────────────────────────

    /// Load (or replace) the anchor layout.
    void set_anchors(const std::vector<AnchorDef>& anchors);

    // ── RSSI ingestion ────────────────────────────────────────────────────────

    /// Feed a raw RSSI reading from a scanning anchor (observer_ap_id) that
    /// has observed target_id at rssi_dbm.  Runs through the rolling filter.
    void feed_rssi(const std::string& observer_ap_id,
                   const std::string& target_id,
                   double             rssi_dbm);

    /// Batch version of feed_rssi.
    /// Accepts the same {ap_id: {device_id: rssi_dbm}} map that Python MQTT
    /// handlers and RSSIFilter::process() already produce, so no Python-side
    /// loop is needed.  All valid readings are processed atomically under one
    /// lock acquisition.
    void feed_rssi_batch(const RSSIFilter::RSSIMap& batch);

    /// Feed a raw RSSI reading between two anchors.
    /// observer_id is the scanning anchor; target_id is the anchor whose
    /// beacon was detected.  Used by P0 calibration and variance detection.
    void feed_inter_anchor(const std::string& observer_id,
                           const std::string& target_id,
                           double             rssi_dbm);

    /// Batch version of feed_inter_anchor.
    /// Same {observer_id: {target_anchor_id: rssi_dbm}} map format.
    void feed_inter_anchor_batch(const RSSIFilter::RSSIMap& batch);

    // ── Position output ───────────────────────────────────────────────────────

    /// Compute and return the current best-estimate position for target_id.
    /// Requires at least 2 anchors to have valid smoothed RSSI for this target.
    /// Falls back to room centre if not enough data exists yet.
    /// Result is cached; use get_last_result() to retrieve without recomputing.
    AnchorPosResult get_position(const std::string& target_id);

    /// Compute and return positions for ALL targets that have received at least
    /// one valid RSSI reading.  Returns {target_id: AnchorPosResult}.
    /// This is the primary output path for the FastAPI polling endpoint —
    /// one call covers every active device without the caller knowing IDs.
    std::unordered_map<std::string, AnchorPosResult> get_all_positions();

    /// Return the last computed position for target_id from the internal cache
    /// WITHOUT re-running trilateration.  Use this for high-frequency HTTP polls
    /// where recomputing on every request is unnecessary.
    /// Returns a zeroed AnchorPosResult with confidence=0 if no result exists yet.
    AnchorPosResult get_last_result(const std::string& target_id) const;

    /// Return the list of target IDs that currently have at least one valid
    /// smoothed RSSI reading (i.e. targets that are actively being tracked).
    std::vector<std::string> get_known_targets() const;

    // ── State management ──────────────────────────────────────────────────────

    /// Evict all cached RSSI and position data for a single target device.
    /// Call when a device is no longer expected (e.g. it left the room).
    void clear_target(const std::string& target_id);

    /// Evict all target device state.  Anchor layout and calibration data
    /// (inter-anchor RSSI, P0 estimates, weights) are preserved.
    void clear_all_targets();

    // ── Calibration ───────────────────────────────────────────────────────────

    /// Return the calibrated receiver-side P0 (reference RSSI at 1 m) for a
    /// given observer anchor, derived from inter-anchor observations with
    /// known geometry.
    ///
    /// Algorithm: for every other anchor B that anchor_id has observed,
    ///   P0_estimate = smoothed_RSSI(anchor_id→B) + 10 * n_B * log10(dist(A,B))
    /// The average across all observed peers is returned.
    /// Falls back to AnchorDef::rssi_at_1m when no inter-anchor data exists.
    double get_rssi_at_1m(const std::string& anchor_id) const;

    /// Return calibrated P0 values for all anchors (keyed by anchor id).
    std::unordered_map<std::string, double> get_all_p0() const;

    // ── Variance / confidence ─────────────────────────────────────────────────

    /**
     * Inter-anchor variance detection.
     *
     * For every anchor pair (A observes B) in ia_smoothed_:
     *   1. measured_dist = rssi_to_distance_m(ia_smoothed[A][B], P0_B, n_B)
     *      (uses B's static tx characteristics to control for hardware bias)
     *   2. true_dist = ||A.pos - B.pos||₂  (from configured geometry)
     *   3. error_m = |measured_dist - true_dist|
     *   4. weight  = 1 / (1 + error_m²)  — inverse-square decay
     *      (0 m error → 1.0,  1 m → 0.5,  3 m → 0.1)
     *   anchor_weights_[A] = mean(weight) across all observed peers.
     *
     * Calls update_anchor_weights_locked() internally.
     * get_all_positions() also calls it automatically each cycle, so
     * explicit calls are only needed for diagnostic read-outs.
     *
     * @returns Updated {anchor_id → weight} map.
     */
    std::unordered_map<std::string, double> rssi_variance_detection();

    /**
     * Returns the latest smoothed RSSI readings for all tracked targets.
     * Shape: {ap_id → {target_id → rssi_dbm}}  (mirrors target_smoothed_).
     * Used by Python to compute per-anchor distance estimates for the UI overlay.
     */
    RSSIFilter::RSSIMap get_target_rssi_cache() const;

    /// Return current per-anchor confidence weights (read-only snapshot).
    std::unordered_map<std::string, double> get_anchor_weights() const;

    // ── Diagnostics ───────────────────────────────────────────────────────────

    /// Return the id → (x, y) lookup table for the currently loaded anchors.
    std::unordered_map<std::string, std::pair<double, double>> create_anchor_map() const;

private:
    // ── Internal data per distance candidate ─────────────────────────────────
    struct AnchorDist {
        std::string id;
        double x, y;
        double dist_m;
    };

    // Build the per-anchor distance list for target_id from cached smoothed RSSI.
    std::vector<AnchorDist> rssi_to_dist_result(const std::string& target_id) const;

    // Run the trilateration solve with current distances + anchor weights.
    AnchorPosResult trilateration_solver(const std::vector<AnchorDist>& candidates) const;

    // Update anchor_weights_ from ia_smoothed_.  Caller MUST hold mu_.
    void update_anchor_weights_locked();

    // ── Members ───────────────────────────────────────────────────────────────
    int    window_size_;
    double max_dist_m_;
    double room_w_, room_h_;

    std::vector<AnchorDef>                     anchors_;
    std::unordered_map<std::string, AnchorDef> anchor_map_;     // id → AnchorDef

    RSSIFilter  filter_;      // Target-scanning RSSI: feed(ap_id, target_id, rssi)
    RSSIFilter  ia_filter_;   // Inter-anchor RSSI:    feed(observer_id, target_id, rssi)

    // Smoothed-value caches — populated by the feed methods.
    // Provides read-only access to current smoothed values without corrupting
    // filter history (RSSIFilter::feed always pushes a new sample).
    std::unordered_map<std::string,
        std::unordered_map<std::string, double>> target_smoothed_;  // [ap_id][target_id]
    std::unordered_map<std::string,
        std::unordered_map<std::string, double>> ia_smoothed_;       // [observer_id][target_id]

    // Per-anchor confidence weights; default 1.0, updated by rssi_variance_detection().
    std::unordered_map<std::string, double> anchor_weights_;

    // Last computed position per target device.
    std::unordered_map<std::string, AnchorPosResult> last_result_;

    mutable std::mutex mu_;
};

} // namespace capstone
