cmake_minimum_required(VERSION 3.16)
project(capstone_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# ── pybind11 ────────────────────────────────────────────────────────────────
# Prefer the pip-installed pybind11 (python3 -m pybind11 --cmakedir)
# Falls back to a vendored copy in extern/pybind11 if present
find_package(pybind11 CONFIG QUIET)
if(NOT pybind11_FOUND)
    if(EXISTS "${CMAKE_SOURCE_DIR}/extern/pybind11/CMakeLists.txt")
        add_subdirectory(extern/pybind11)
    else()
        message(FATAL_ERROR
            "pybind11 not found. Install via: pip install pybind11\n"
            "  or clone into extern/pybind11")
    endif()
endif()

# ── Source files ────────────────────────────────────────────────────────────
set(CORE_SOURCES
    core_cpp/math_core/trilateration.cpp
    core_cpp/math_core/geometry.cpp
    core_cpp/math_core/knn_matrix.cpp
    core_cpp/math_core/signal_filters.cpp
    core_cpp/parsers/telnet_parser.cpp
    core_cpp/engines/rssi_engine.cpp
)

# ── Shared pybind11 module → capstone_core.<platform>.so ────────────────────
pybind11_add_module(capstone_core
    core_cpp/bindings.cpp
    ${CORE_SOURCES}
)

target_include_directories(capstone_core PRIVATE
    ${CMAKE_SOURCE_DIR}/core_cpp
    ${CMAKE_SOURCE_DIR}/core_cpp/math_core
    ${CMAKE_SOURCE_DIR}/core_cpp/parsers
    ${CMAKE_SOURCE_DIR}/core_cpp/engines
)

# Copy the built .so into src_python/ so Python can import it directly
add_custom_command(TARGET capstone_core POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy
        $<TARGET_FILE:capstone_core>
        ${CMAKE_SOURCE_DIR}/src_python/$<TARGET_FILE_NAME:capstone_core>
    COMMENT "Copying capstone_core module to src_python/"
)
