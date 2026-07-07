/**
 * OTA firmware updates over HTTP(S) — esp_https_ota with dual app slots
 * and bootloader rollback (CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE).
 *
 * Flow: the backend/operator publishes {"action":"OTA","url":"<http(s)>"}
 * on the gateway's command topic → ota_update_start() downloads into the
 * passive slot and reboots. The new image boots as PENDING_VERIFY; once the
 * gateway proves itself (MQTT connected), main.c calls
 * ota_mark_valid_if_pending() to cancel rollback. If the new image crashes
 * or never reaches the broker, the next reboot rolls back to the previous
 * slot automatically.
 */
#pragma once

#include <stdbool.h>
#include "esp_err.h"

/* Callback for coarse OTA lifecycle events ("OTA_STARTED", "OTA_FAILED",
 * "OTA_OK_REBOOTING"); main.c publishes them to the alarms topic. */
typedef void (*ota_event_cb_t)(const char *event);

void ota_update_init(ota_event_cb_t event_cb);

/* Kick off an OTA from `url` in a background task. Returns
 * ESP_ERR_INVALID_STATE if one is already running, ESP_ERR_INVALID_ARG for
 * a non-http(s) URL. */
esp_err_t ota_update_start(const char *url);

bool ota_update_in_progress(void);

/* On the first boot of a freshly-OTA'd image (PENDING_VERIFY), mark it
 * valid and cancel rollback. Call once the gateway is demonstrably healthy
 * — we use "connected + authenticated to the MQTT broker" as the bar. */
void ota_mark_valid_if_pending(void);
