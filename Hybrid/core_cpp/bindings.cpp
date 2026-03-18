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
}
