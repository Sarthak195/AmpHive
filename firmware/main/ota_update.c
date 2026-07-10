#include "ota_update.h"

#include <stdlib.h>
#include <string.h>

#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "esp_app_desc.h"
#include "esp_crt_bundle.h"
#include "esp_https_ota.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_system.h"

static const char *TAG = "ota_update";

static ota_event_cb_t s_event_cb = NULL;
static volatile bool s_in_progress = false;

static void emit(const char *event)
{
    ESP_LOGI(TAG, "event: %s", event);
    if (s_event_cb) {
        s_event_cb(event);
    }
}

void ota_update_init(ota_event_cb_t event_cb)
{
    s_event_cb = event_cb;
}

bool ota_update_in_progress(void)
{
    return s_in_progress;
}

static void ota_task(void *arg)
{
    char *url = (char *)arg;
    emit("OTA_STARTED");
    ESP_LOGI(TAG, "downloading from %s (running %s)",
             url, esp_app_get_description()->version);

    esp_http_client_config_t http_cfg = {
        .url = url,
        .timeout_ms = 30000,
        .keep_alive_enable = true,
        /* Validate HTTPS image hosts against the built-in Mozilla CA bundle
           (CONFIG_MBEDTLS_CERTIFICATE_BUNDLE). Since direct-MQTT devices fetch
           OTA images across the public internet, serve them from an https://
           URL (GitHub release, GCS, a TLS-fronted VM) so the download is
           authenticated + encrypted. Plain http:// is still permitted
           (CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP) for LAN/overlay hosting, but it is
           MITM-able on untrusted networks — prefer https there. The image is
           additionally app-validated and rollback-protected by esp_https_ota. */
        .crt_bundle_attach = esp_crt_bundle_attach,
    };
    esp_https_ota_config_t ota_cfg = {
        .http_config = &http_cfg,
    };

    esp_https_ota_handle_t handle = NULL;
    esp_err_t err = esp_https_ota_begin(&ota_cfg, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_https_ota_begin failed: %s", esp_err_to_name(err));
        goto fail;
    }

    /* Log what we're about to install (also rejects non-app payloads early). */
    esp_app_desc_t incoming;
    if (esp_https_ota_get_img_desc(handle, &incoming) == ESP_OK) {
        ESP_LOGI(TAG, "image version: %s (project %s)",
                 incoming.version, incoming.project_name);
    }

    int total = esp_https_ota_get_image_size(handle);
    int last_logged_pct = -10;
    while ((err = esp_https_ota_perform(handle)) == ESP_ERR_HTTPS_OTA_IN_PROGRESS) {
        if (total > 0) {
            int read = esp_https_ota_get_image_len_read(handle);
            int pct = (int)((int64_t)read * 100 / total);
            if (pct >= last_logged_pct + 10) {
                ESP_LOGI(TAG, "progress: %d%% (%d/%d bytes)", pct, read, total);
                last_logged_pct = pct;
            }
        }
    }

    if (err != ESP_OK || !esp_https_ota_is_complete_data_received(handle)) {
        ESP_LOGE(TAG, "download failed/incomplete: %s", esp_err_to_name(err));
        esp_https_ota_abort(handle);
        goto fail;
    }

    err = esp_https_ota_finish(handle); /* validates image + sets boot slot */
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "esp_https_ota_finish failed: %s", esp_err_to_name(err));
        goto fail;
    }

    emit("OTA_OK_REBOOTING");
    ESP_LOGI(TAG, "update written; rebooting into the new slot");
    free(url);
    vTaskDelay(pdMS_TO_TICKS(2000)); /* let the MQTT publish drain */
    esp_restart();

fail:
    emit("OTA_FAILED");
    free(url);
    s_in_progress = false;
    vTaskDelete(NULL);
}

esp_err_t ota_update_start(const char *url)
{
    if (s_in_progress) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!url || strncmp(url, "http", 4) != 0) {
        return ESP_ERR_INVALID_ARG;
    }
    char *url_copy = strdup(url);
    if (!url_copy) {
        return ESP_ERR_NO_MEM;
    }
    s_in_progress = true;
    /* 8K stack: TLS-capable HTTP client + flash writes run here. */
    if (xTaskCreate(ota_task, "ota_update", 8192, url_copy, 5, NULL) != pdPASS) {
        free(url_copy);
        s_in_progress = false;
        return ESP_ERR_NO_MEM;
    }
    return ESP_OK;
}

void ota_mark_valid_if_pending(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state;
    if (esp_ota_get_state_partition(running, &state) == ESP_OK &&
        state == ESP_OTA_IMG_PENDING_VERIFY) {
        ESP_LOGI(TAG, "first boot after OTA reached the broker - marking image valid");
        esp_ota_mark_app_valid_cancel_rollback();
    }
}
