cmake_minimum_required(VERSION 3.16)

# Explicit firmware version (reported in the MQTT online status and used to
# verify OTA updates took). Bump on every OTA-shipped change.
set(PROJECT_VER "1.1.1")

# Include standard ESP-IDF build logic
include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(amphive-gateway)
