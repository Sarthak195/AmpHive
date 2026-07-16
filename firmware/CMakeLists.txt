cmake_minimum_required(VERSION 3.16)

# Explicit firmware version (reported in the MQTT online status and used to
# verify OTA updates took). Bump on every OTA-shipped change.
set(PROJECT_VER "2.1.0-direct")

# Include standard ESP-IDF build logic
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(amphive-gateway)
