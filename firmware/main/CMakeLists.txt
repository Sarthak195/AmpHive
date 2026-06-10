idf_component_register(SRCS "main.c" "tapo_protocol.c"
                       INCLUDE_DIRS "."
                       REQUIRES microlink mqtt nvs_flash esp_wifi esp_http_server)
