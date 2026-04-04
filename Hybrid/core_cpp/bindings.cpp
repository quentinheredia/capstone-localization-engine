/**
 * pybind11 bindings for capstone_core
 *
 * Exposes all C++ math, parsing, and engine modules to Python.
 * After building, Python imports this as:
 *     import capstone_core as cc
 */

#include <pybind11/pybind11.h>
#include <pybind11/stl.h>           // automatic std::vector, std::map, etc.
#include <pybind11/stl_bind.h>

#include "math_core/trilateration.h"
#include "math_core/geometry.h"
#include "math_core/knn_matrix.h"
#include "math_core/signal_filters.h"
#include "parsers/telnet_parser.h"
#include "engines/rssi_engine.h"
#include "engines/accuracy_engine.h"
#include "engines/anchor_positioning.h"

namespace py = pybind11;
using namespace capstone;

PYBIND11_MODULE(capstone_core, m) {
    m.doc() = "Capstone Localization — C++ core engines (pybind11)";

    // ═══════════════════════════════════════════════════════════════════════
    //  math_core.trilateration
    // ═══════════════════════════════════════════════════════════════════════
    m.def("rssi_to_distance_m", &rssi_to_distance_m,
          py::arg("rssi_dbm"), py::arg("p0_dbm"), py::arg("n"),
          "Convert RSSI (dBm) to distance (m) via path-loss model. Clamped [0.05, 50].");

    m.def("bounded_trilaterate", &bounded_trilaterate,
          py::arg("anchors"), py::arg("dists"),
          py::arg("room_w"), py::arg("room_h"),
          py::arg("init_x"), py::arg("init_y"),
          "Gradient-descent trilateration bounded within room dimensions.");

    m.def("refined_trilaterate", &refined_trilaterate,
          py::arg("anchors"), py::arg("dists"),
          py::arg("room_w"), py::arg("room_h"),
          "Sanitize + weighted-centroid guess + bounded trilaterate.");

    // ═══════════════════════════════════════════════════════════════════════
    //  math_core.geometry
    // ═══════════════════════════════════════════════════════════════════════
    m.def("point_in_polygon", &point_in_polygon,
          py::arg("px"), py::arg("py"), py::arg("polygon"),
          "Ray-casting point-in-polygon test.");

    py::class_<ClampResult>(m, "ClampResult")
        .def_readonly("x", &ClampResult::x)
        .def_readonly("y", &ClampResult::y)
        .def_readonly("clamped", &ClampResult::clamped);

    m.def("boundary_clamp", &boundary_clamp,
          py::arg("x"), py::arg("y"),
          py::arg("room_w"), py::arg("room_h"),
          py::arg("margin"),
          "Clamp (x,y) within margin of room walls.");

    py::class_<RoomDef>(m, "RoomDef")
        .def(py::init<>())
        .def_readwrite("name",      &RoomDef::name)
        .def_readwrite("center_x",  &RoomDef::center_x)
        .def_readwrite("center_y",  &RoomDef::center_y)
        .def_readwrite("polygon",   &RoomDef::polygon);

    py::class_<RoomClassification>(m, "RoomClassification")
        .def_readonly("room_name",  &RoomClassification::room_name)
        .def_readonly("confidence", &RoomClassification::confidence)
        .def_readonly("x",          &RoomClassification::x)
        .def_readonly("y",          &RoomClassification::y);

    m.def("classify_position", &classify_position,
          py::arg("est_x"), py::arg("est_y"),
          py::arg("rooms"),
          py::arg("room_w"), py::arg("room_h"),
          py::arg("clamp_margin"), py::arg("max_dist_high_conf"),
          "Clamp + polygon test + confidence scoring.");

    // ═══════════════════════════════════════════════════════════════════════
    //  math_core.knn_matrix
    // ═══════════════════════════════════════════════════════════════════════
    py::class_<RadioMapEntry>(m, "RadioMapEntry")
        .def(py::init<>())
        .def_readwrite("room",    &RadioMapEntry::room)
        .def_readwrite("vectors", &RadioMapEntry::vectors);

    py::class_<KNNResult>(m, "KNNResult")
        .def_readonly("room",       &KNNResult::room)
        .def_readonly("confidence", &KNNResult::confidence);

    m.def("euclidean_rssi_distance", &euclidean_rssi_distance,
          py::arg("a"), py::arg("b"),
          "Euclidean distance between two RSSI vectors. Returns (dist, common_aps).");

    m.def("knn_fingerprint_match", &knn_fingerprint_match,
          py::arg("live_vector"), py::arg("radio_map"),
          py::arg("k") = 3, py::arg("confidence_baseline") = 30.0,
          "K-Nearest-Neighbours room match against a radio map.");

    // ═══════════════════════════════════════════════════════════════════════
    //  math_core.signal_filters (Stateful)
    // ═══════════════════════════════════════════════════════════════════════
    py::class_<RSSIFilter>(m, "RSSIFilter")
        .def(py::init<int, double>(),
             py::arg("window_size"), py::arg("noise_floor_dbm"))
        .def("feed", &RSSIFilter::feed,
             py::arg("ap_id"), py::arg("device_id"), py::arg("raw_rssi"),
             "Feed one RSSI reading, get smoothed value (-999.0 if filtered).")
        .def("process", &RSSIFilter::process,
             py::arg("raw"),
             "Batch process: {ap: {dev: rssi}} -> {ap: {dev: smoothed}}.")
        .def("clear", &RSSIFilter::clear,
             "Reset all rolling history.")
        .def_property_readonly("window_size", &RSSIFilter::window_size)
        .def_property_readonly("noise_floor", &RSSIFilter::noise_floor);

    // ═══════════════════════════════════════════════════════════════════════
    //  parsers.telnet_parser
    // ═══════════════════════════════════════════════════════════════════════
    py::class_<APScanRow>(m, "APScanRow")
        .def_readonly("bssid",  &APScanRow::bssid)
        .def_readonly("ssid",   &APScanRow::ssid)
        .def_readonly("len",    &APScanRow::len)
        .def_readonly("mode",   &APScanRow::mode)
        .def_readonly("ch",     &APScanRow::ch)
        .def_readonly("signal", &APScanRow::signal)
        .def_readonly("enc",    &APScanRow::enc)
        .def_readonly("type",   &APScanRow::type);

    m.def("parse_apscan_table", &parse_apscan_table,
          py::arg("text"),
          "Parse EAP350 APSCAN output -> list of APScanRow.");

    m.def("parse_apscan_table_dicts", &parse_apscan_table_dicts,
          py::arg("text"),
          "Parse EAP350 APSCAN output -> list of dicts (Python-friendly).");

    // ═══════════════════════════════════════════════════════════════════════
    //  engines.rssi_engine (Stateful)
    // ═══════════════════════════════════════════════════════════════════════
    py::class_<RSSIEngine::APDef>(m, "APDef")
        .def(py::init<>())
        .def_readwrite("id", &RSSIEngine::APDef::id)
        .def_readwrite("x",  &RSSIEngine::APDef::x)
        .def_readwrite("y",  &RSSIEngine::APDef::y);

    py::class_<RSSIEngine::TargetDef>(m, "TargetDef")
        .def(py::init<>())
        .def_readwrite("ssid",        &RSSIEngine::TargetDef::ssid)
        .def_readwrite("rssi_at_1m",  &RSSIEngine::TargetDef::rssi_at_1m)
        .def_readwrite("path_loss_n", &RSSIEngine::TargetDef::path_loss_n);

    py::class_<RSSIEngine::LocalizationResult>(m, "LocalizationResult")
        .def_readonly("device_id",  &RSSIEngine::LocalizationResult::device_id)
        .def_readonly("room",       &RSSIEngine::LocalizationResult::room)
        .def_readonly("confidence", &RSSIEngine::LocalizationResult::confidence)
        .def_readonly("x",          &RSSIEngine::LocalizationResult::x)
        .def_readonly("y",          &RSSIEngine::LocalizationResult::y);

    py::class_<RSSIEngine>(m, "RSSIEngine")
        .def(py::init<int, double, int, double, double, double, double>(),
             py::arg("window_size"), py::arg("noise_floor_dbm"),
             py::arg("min_aps"), py::arg("clamp_margin"),
             py::arg("max_dist_conf"),
             py::arg("room_w"), py::arg("room_h"))
        .def("set_aps",   &RSSIEngine::set_aps,   py::arg("aps"))
        .def("set_rooms", &RSSIEngine::set_rooms,  py::arg("rooms"))
        .def("process_cycle", &RSSIEngine::process_cycle,
             py::arg("raw_rssi"), py::arg("targets"),
             "Run one full localization cycle: filter -> trilaterate -> classify.");

    // ═══════════════════════════════════════════════════════════════════════
    //  engines.accuracy_engine — Analytics Dashboard backend
    // ═══════════════════════════════════════════════════════════════════════

    // Module-level constant so Python can read the hard outlier cap without
    // duplicating the magic number.
    m.attr("ACCURACY_HARD_CAP_M") = capstone::ACCURACY_HARD_CAP_M;

    // ── ErrorSample (read-only DTO) ────────────────────────────────────────
    py::class_<ErrorSample>(m, "ErrorSample")
        .def_readonly("timestamp_ms",  &ErrorSample::timestamp_ms,
                      "Unix epoch milliseconds when the estimate was recorded.")
        .def_readonly("error_m",       &ErrorSample::error_m,
                      "Euclidean error in metres, hard-capped at ACCURACY_HARD_CAP_M.")
        .def_readonly("raw_error_m",   &ErrorSample::raw_error_m,
                      "Uncapped Euclidean error in metres (for failure percentage).")
        .def_readonly("est_x",         &ErrorSample::est_x,
                      "Estimated X coordinate (metres, same frame as ground truth).")
        .def_readonly("est_y",         &ErrorSample::est_y,
                      "Estimated Y coordinate (metres).")
        .def_readonly("scenario",      &ErrorSample::scenario,
                      "Observer-tagged scenario label, e.g. 'LOS', 'NLOS'.")
        .def_readonly("is_outlier",    &ErrorSample::is_outlier,
                      "True when the raw error exceeded ACCURACY_HARD_CAP_M.");

    // ── AccuracyEngine (stateful) ──────────────────────────────────────────
    py::class_<AccuracyEngine>(m, "AccuracyEngine",
            R"doc(
Rolling error accumulator for multi-algorithm accuracy comparison.

Usage::

    eng = cc.AccuracyEngine(max_buf=500)
    eng.set_ground_truth(3.0, 7.5)
    eng.push_estimate("rssi", 3.1, 7.3, "LOS")
    samples = eng.get_buffer("rssi")  # list[ErrorSample]
)doc")
        .def(py::init<int>(), py::arg("max_buf") = 500,
             "Create engine with rolling buffer of `max_buf` samples per method.")
        // Ground truth
        .def("set_ground_truth", &AccuracyEngine::set_ground_truth,
             py::arg("x"), py::arg("y"),
             "Set the reference position in metres.")
        .def("get_ground_truth", &AccuracyEngine::get_ground_truth,
             "Returns (gt_x, gt_y) as a tuple.")
        // Ingestion
        .def("push_estimate", &AccuracyEngine::push_estimate,
             py::arg("method"), py::arg("est_x"), py::arg("est_y"),
             py::arg("scenario") = "", py::arg("ts_ms") = 0,
             "Record a new estimate; error is computed immediately against GT.")
        // Retrieval
        .def("get_buffer", &AccuracyEngine::get_buffer,
             py::arg("method"),
             "Return a snapshot of ErrorSample records for the given method.")
        .def("method_keys", &AccuracyEngine::method_keys,
             "List of method keys with at least one sample (sorted).")
        .def("clear_method", &AccuracyEngine::clear_method,
             py::arg("method"), "Discard all samples for one method.")
        .def("clear_all", &AccuracyEngine::clear_all,
             "Discard all samples for all methods.")
        .def_property_readonly("max_buf", &AccuracyEngine::max_buf,
             "Maximum buffer size per method (set at construction).");

    // ═══════════════════════════════════════════════════════════════════════
    //  engines.anchor_positioning — Self-calibrating anchor-assisted engine
    // ═══════════════════════════════════════════════════════════════════════

    // ── AnchorDef ─────────────────────────────────────────────────────────────
    // Anchor data originates in Python (parsed from config.yaml / the JS API).
    // Two construction patterns are supported:
    //   a) Keyword-argument constructor — most natural when building from a
    //      Python dict that mirrors the config schema:
    //        a = cc.AnchorDef(id="AP1", x=0.0, y=0.0, rssi_at_1m=-60.0, n=2.5)
    //   b) Default-construct + assign — kept for backward compatibility.
    py::class_<AnchorDef>(m, "AnchorDef")
        .def(py::init<>(),
             "Default-construct (all fields at their zero/default values).")
        .def(py::init([](std::string id, double x, double y,
                         double rssi_at_1m, double path_loss_n) {
                 AnchorDef a;
                 a.id          = std::move(id);
                 a.x           = x;
                 a.y           = y;
                 a.rssi_at_1m  = rssi_at_1m;
                 a.path_loss_n = path_loss_n;
                 return a;
             }),
             py::arg("id"),
             py::arg("x")           = 0.0,
             py::arg("y")           = 0.0,
             py::arg("rssi_at_1m")  = -60.0,
             py::arg("path_loss_n") = 2.5,
             "Keyword-argument constructor — build directly from a config dict.")
        .def_readwrite("id",          &AnchorDef::id)
        .def_readwrite("x",           &AnchorDef::x)
        .def_readwrite("y",           &AnchorDef::y)
        .def_readwrite("rssi_at_1m",  &AnchorDef::rssi_at_1m,
             "Fallback P0 (dBm) used before inter-anchor calibration fires.")
        .def_readwrite("path_loss_n", &AnchorDef::path_loss_n,
             "Path-loss exponent (indoors: 2.0–3.5).")
        .def("__repr__", [](const AnchorDef& a) {
            return "AnchorDef(id='" + a.id + "', x=" + std::to_string(a.x)
                   + ", y=" + std::to_string(a.y)
                   + ", rssi_at_1m=" + std::to_string(a.rssi_at_1m)
                   + ", path_loss_n=" + std::to_string(a.path_loss_n) + ")";
        });

    // ── AnchorPosResult ───────────────────────────────────────────────────────
    // Read-only result DTO returned to Python and forwarded to the frontend
    // via FastAPI as JSON.  Use result.x / result.y directly, or call
    // to_dict() to get a plain Python dict for JSON serialisation.
    py::class_<AnchorPosResult>(m, "AnchorPosResult")
        .def_readonly("x",          &AnchorPosResult::x)
        .def_readonly("y",          &AnchorPosResult::y)
        .def_readonly("confidence", &AnchorPosResult::confidence,
             "Heuristic 0..1 based on anchor count and variance weights.")
        .def_readonly("n_anchors",  &AnchorPosResult::n_anchors,
             "Number of anchors that contributed a valid distance estimate.")
        .def("to_dict", [](const AnchorPosResult& r) {
            py::dict d;
            d["x"]          = r.x;
            d["y"]          = r.y;
            d["confidence"] = r.confidence;
            d["n_anchors"]  = r.n_anchors;
            return d;
        }, "Return a plain Python dict — convenient for FastAPI JSON responses.")
        .def("__repr__", [](const AnchorPosResult& r) {
            return "AnchorPosResult(x=" + std::to_string(r.x)
                   + ", y=" + std::to_string(r.y)
                   + ", confidence=" + std::to_string(r.confidence)
                   + ", n_anchors=" + std::to_string(r.n_anchors) + ")";
        });

    // ── AnchorPosEngine ───────────────────────────────────────────────────────
    py::class_<AnchorPosEngine>(m, "AnchorPosEngine",
            R"doc(
Self-calibrating anchor-assisted positioning engine.

Data-flow summary
-----------------
  config.yaml  →  Python  →  set_anchors([cc.AnchorDef(...), ...])
  MQTT scan    →  Python  →  feed_rssi_batch({ap_id: {dev_id: rssi}})
  Anchor beacons→ Python  →  feed_inter_anchor_batch({obs: {target: rssi}})
  FastAPI poll →  Python  →  get_all_positions()  →  JSON  →  Frontend

Usage::

    eng = cc.AnchorPosEngine(window_size=5, max_dist_m=15.0,
                             room_w=8.62, room_h=13.91)

    # Build anchors from config dict (kwarg constructor):
    anchors = [cc.AnchorDef(id=a["id"], x=a["x"], y=a["y"],
                             rssi_at_1m=a.get("rssi_at_1m", -60.0),
                             path_loss_n=a.get("path_loss_n", 2.5))
               for a in cfg["anchors"]]
    eng.set_anchors(anchors)

    # Feed one full MQTT scan cycle (batch — same dict the RSSI handler produces):
    eng.feed_rssi_batch(raw_rssi_map)          # {ap_id: {device_id: rssi_dbm}}
    eng.feed_inter_anchor_batch(ia_rssi_map)   # {observer_id: {anchor_id: rssi_dbm}}

    # Return all tracked device positions to the frontend:
    positions = eng.get_all_positions()        # {target_id: AnchorPosResult}
    return {tid: r.to_dict() for tid, r in positions.items()}
)doc")
        .def(py::init<int, double, double, double>(),
             py::arg("window_size"), py::arg("max_dist_m"),
             py::arg("room_w"),      py::arg("room_h"))
        // ── Setup
        .def("set_anchors", &AnchorPosEngine::set_anchors,
             py::arg("anchors"),
             "Load (or replace) the anchor layout from a list of AnchorDef objects.")
        // ── Single-reading ingestion
        .def("feed_rssi", &AnchorPosEngine::feed_rssi,
             py::arg("observer_ap_id"), py::arg("target_id"), py::arg("rssi_dbm"),
             "Feed one raw RSSI reading from a scanning anchor for a target device.")
        .def("feed_inter_anchor", &AnchorPosEngine::feed_inter_anchor,
             py::arg("observer_id"), py::arg("target_id"), py::arg("rssi_dbm"),
             "Feed one raw RSSI observation between two anchors.")
        // ── Batch ingestion (preferred — matches Python dict output from MQTT handler)
        .def("feed_rssi_batch", &AnchorPosEngine::feed_rssi_batch,
             py::arg("batch"),
             "Batch-feed {ap_id: {device_id: rssi_dbm}} under one lock. "
             "Accepts the same dict format that RSSIFilter.process() returns.")
        .def("feed_inter_anchor_batch", &AnchorPosEngine::feed_inter_anchor_batch,
             py::arg("batch"),
             "Batch-feed {observer_id: {anchor_id: rssi_dbm}} for inter-anchor calibration.")
        // ── Position output
        .def("get_position", &AnchorPosEngine::get_position,
             py::arg("target_id"),
             "Compute and cache position for one target device.")
        .def("get_all_positions", &AnchorPosEngine::get_all_positions,
             "Compute positions for ALL known targets. "
             "Returns {target_id: AnchorPosResult}. Primary FastAPI output path.")
        .def("get_last_result", &AnchorPosEngine::get_last_result,
             py::arg("target_id"),
             "Return last cached position without recomputing (fast poll path).")
        .def("get_known_targets", &AnchorPosEngine::get_known_targets,
             "Return sorted list of target IDs currently being tracked.")
        // ── State management
        .def("clear_target", &AnchorPosEngine::clear_target,
             py::arg("target_id"),
             "Evict RSSI cache and last result for one target device.")
        .def("clear_all_targets", &AnchorPosEngine::clear_all_targets,
             "Evict all target state; preserves anchor layout and calibration.")
        // ── Calibration
        .def("get_rssi_at_1m", &AnchorPosEngine::get_rssi_at_1m,
             py::arg("anchor_id"),
             "Return calibrated P0 for anchor_id derived from inter-anchor geometry.")
        .def("get_all_p0", &AnchorPosEngine::get_all_p0,
             "Return {anchor_id: calibrated_p0} for all anchors.")
        // ── Variance (stub)
        .def("rssi_variance_detection", &AnchorPosEngine::rssi_variance_detection,
             "Run inter-anchor variance detection; updates anchor weights. (STUB)")
        .def("get_anchor_weights", &AnchorPosEngine::get_anchor_weights,
             "Return current {anchor_id: weight} snapshot (read-only).")
        // ── Diagnostics
        .def("create_anchor_map", &AnchorPosEngine::create_anchor_map,
             "Return {anchor_id: (x, y)} for the currently loaded anchors.");
}
