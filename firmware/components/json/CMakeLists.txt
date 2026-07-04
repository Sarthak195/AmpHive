# Local cJSON component, vendored as "json".
#
# ESP-IDF v6.0 removed the built-in `json` component from core (it is now only
# on the component registry). The microlink component REQUIRES `json` and
# #includes "cJSON.h", so we vendor cJSON v1.7.18 (MIT) here to keep the project
# buildable on ESP-IDF v6 without a network dependency at configure time.
idf_component_register(SRCS "cJSON.c"
                       INCLUDE_DIRS ".")
