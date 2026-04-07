cmake_minimum_required(VERSION 3.16)

# Set target chip before including IDF
set(IDF_TARGET "esp32s3")

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(tof_anchor)
