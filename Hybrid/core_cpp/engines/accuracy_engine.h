#pragma once

/**
 * accuracy_engine.h
 *
 * C++ backend for the Analytics Dashboard.
 *
 * Responsibilities
 * ----------------
 *   1. Store the current Ground Truth position (gt_x_, gt_y_).
 *   2. Accept per-method position estimates; compute Euclidean error immediately.
 *   3. Maintain a fixed-size rolling buffer (std::deque, max = max_buf_) of
 *      ErrorSample records per method to bound memory use.
 *   4. Expose thread-safe bulk-read of a buffer snapshot for the Python layer.
 *
 * Thread safety
 * -------------
 *   All public methods are guarded by a single std::mutex so pybind11 can call
 *   from Python threads without data races.
 *
 * Design notes
 * ------------
 *   - Timestamps are stored as milliseconds since the Unix epoch (int64_t).
 *     The caller supplies the timestamp; if 0 is passed the engine stamps now().
 *   - Scenarios (e.g. "LOS", "NLOS", "Facing North") are stored per sample so
 *     the Python layer can filter CDF curves by scenario without data loss.
 *   - Error values are clamped to [0, HARD_CAP_M] before storage.  Anything
 *     beyond HARD_CAP_M is stored at HARD_CAP_M and flagged is_outlier=true.
 *     This keeps the CDF X-axis readable while still counting failures.
 */

#include <cstdint>
#include <deque>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>
#include <chrono>
#include <cmath>

namespace capstone {

/// Hard outlier cap in metres.  Errors beyond this are bucketed as failures.
constexpr double ACCURACY_HARD_CAP_M = 10.0;

// ─────────────────────────────────────────────────────────────────────────────
/// One error observation for a single algorithm estimate.
// ─────────────────────────────────────────────────────────────────────────────
struct ErrorSample {
    int64_t     timestamp_ms;   ///< Unix epoch ms of the estimate
    double      error_m;        ///< Euclidean distance to ground truth (capped)
    double      raw_error_m;    ///< Uncapped error (for stats / failure %)
    double      est_x;          ///< Estimated X coordinate (m)
    double      est_y;          ///< Estimated Y coordinate (m)
    std::string scenario;       ///< Observer-tagged scenario label
    bool        is_outlier;     ///< true when raw_error_m > ACCURACY_HARD_CAP_M
};

// ─────────────────────────────────────────────────────────────────────────────
/// Per-method rolling error buffer + ground-truth calculator.
// ─────────────────────────────────────────────────────────────────────────────
class AccuracyEngine {
public:
    /**
     * @param max_buf   Maximum samples to keep per method (default 500).
     *                  Oldest samples are dropped when the buffer is full.
     */
    explicit AccuracyEngine(int max_buf = 500);

    // ── Ground truth ─────────────────────────────────────────────────────────

    /// Set the reference position.  Thread-safe.
    void set_ground_truth(double x, double y);

    /// Returns {gt_x, gt_y}.
    std::pair<double, double> get_ground_truth() const;

    // ── Data ingestion ────────────────────────────────────────────────────────

    /**
     * Record a new position estimate for `method`.
     *
     * Calculates error = Euclidean distance from (est_x, est_y) to ground truth,
     * clamps to ACCURACY_HARD_CAP_M, pushes to the rolling deque.
     *
     * @param method    Algorithm name key, e.g. "rssi", "kalman", "tof"
     * @param est_x     Estimated X in metres (same coordinate frame as GT)
     * @param est_y     Estimated Y in metres
     * @param scenario  Observer label, e.g. "LOS", "NLOS", ""
     * @param ts_ms     Timestamp (Unix ms).  Pass 0 to auto-stamp.
     */
    void push_estimate(const std::string& method,
                       double est_x, double est_y,
                       const std::string& scenario = "",
                       int64_t ts_ms = 0);

    // ── Data retrieval ────────────────────────────────────────────────────────

    /// Snapshot of the rolling buffer for `method`.  Returns empty vector if
    /// no data yet.  Caller receives a copy — no lock held after return.
    std::vector<ErrorSample> get_buffer(const std::string& method) const;

    /// List of method keys that have at least one sample.
    std::vector<std::string> method_keys() const;

    /// Clear the buffer for one method.
    void clear_method(const std::string& method);

    /// Clear all buffers.
    void clear_all();

    /// Buffer capacity (set at construction).
    int max_buf() const { return max_buf_; }

private:
    static int64_t _now_ms() {
        using namespace std::chrono;
        return duration_cast<milliseconds>(
            system_clock::now().time_since_epoch()).count();
    }

    int     max_buf_;
    double  gt_x_{ 0.0 };
    double  gt_y_{ 0.0 };

    std::unordered_map<std::string, std::deque<ErrorSample>> buffers_;
    mutable std::mutex mu_;
};

} // namespace capstone
