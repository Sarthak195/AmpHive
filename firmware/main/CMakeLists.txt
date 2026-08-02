idf_component_register(SRCS "main.c" "tapo_protocol.c" "session_nvs.c" "offline_log.c"
                            "ota_update.c"
                       INCLUDE_DIRS "."
                       REQUIRES mqtt nvs_flash esp_wifi esp_http_server
                                esp_http_client mbedtls json
                                esp_https_ota app_update esp_app_format
                       EMBED_TXTFILES "certs/mqtt_ca.crt")
