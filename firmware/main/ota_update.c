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

/* Compare two dotted firmware versions ("MAJOR.MINOR.PATCH[-suffix]") by their
 * numeric components. Returns <0, 0, >0 for a<b, a==b, a>b. Missing/trailing
 * components count as 0 and the "-direct" suffix is ignored. Numeric (not
 * strcmp) so 2.10.0 sorts above 2.9.0. */
static int fw_version_cmp(const char *a, const char *b)
{
    for (int i = 0; i < 3; i++) {
        char *ea, *eb;
        long va = strtol(a, &ea, 10);
        long vb = strtol(b, &eb, 10);
        if (va != vb) {
            return (va < vb) ? -1 : 1;
        }
        a = (*ea == '.') ? ea + 1 : ea;
        b = (*eb == '.') ? eb + 1 : eb;
    }
    return 0;
}

/* Optional OTA host allowlist (defense-in-depth, OFF by default).
 *
 * OTA images are already gated by a mandatory ECDSA app signature
 * (esp_https_ota_finish) plus public-CA TLS, so pinning the host buys little
 * and risks bricking field delivery if the operator moves the image host
 * (images are served from BOTH gs://amphive-fw and the backend's own origin —
 * see docs/FIRMWARE.md §7). It is therefore opt-in: define
 * AMPHIVE_OTA_ALLOWED_HOST_SUFFIX (e.g. ".amphive.app") at build time to require
 * the URL host to end with that suffix. Undefined preserves the historical
 * "any https:// host" behavior. */
#ifdef AMPHIVE_OTA_ALLOWED_HOST_SUFFIX
static bool ota_url_host_allowed(const char *url)
{
    const char *host = url + 8;              /* skip "https://" (caller checked) */
    size_t host_len = strcspn(host, ":/");   /* host ends at port ':' or path '/' */
    size_t suf_len = strlen(AMPHIVE_OTA_ALLOWED_HOST_SUFFIX);
    return host_len >= suf_len &&
           memcmp(host + host_len - suf_len, AMPHIVE_OTA_ALLOWED_HOST_SUFFIX, suf_len) == 0;
}
#endif

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
        /* Validate the HTTPS image host against the built-in Mozilla CA bundle
           (CONFIG_MBEDTLS_CERTIFICATE_BUNDLE). Direct-MQTT devices fetch OTA
           images across the public internet, so https:// is REQUIRED — plain
           http:// is rejected both here (ota_update_start) and by esp_https_ota
           (CONFIG_ESP_HTTPS_OTA_ALLOW_HTTP is off). On top of transport auth,
           the image itself must carry a valid ECDSA app signature
           (CONFIG_SECURE_SIGNED_ON_UPDATE_NO_SECURE_BOOT): esp_https_ota_finish
           refuses an unsigned/forged image even from a compromised host. */
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
        const char *running = esp_app_get_description()->version;
        ESP_LOGI(TAG, "image version: %s (project %s), running %s",
                 incoming.version, incoming.project_name, running);
        /* Anti-rollback floor (software, defense-in-depth): refuse a strictly
           OLDER image so a replayed/compromised OTA can't downgrade the fleet
           to a patched-out bug. This deliberately does NOT burn the
           secure-version eFuse (CONFIG_APP_ANTI_ROLLBACK) — that is irreversible
           and operator-gated. Signed-image verification in esp_https_ota_finish
           still applies on top of this. */
        if (fw_version_cmp(incoming.version, running) < 0) {
            ESP_LOGE(TAG, "rejecting downgrade: incoming %s < running %s",
                     incoming.version, running);
            esp_https_ota_abort(handle);
            goto fail;
        }
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
    /* https only: images traverse the public internet (see ota_task). */
    if (!url || strncmp(url, "https://", 8) != 0) {
        return ESP_ERR_INVALID_ARG;
    }
#ifdef AMPHIVE_OTA_ALLOWED_HOST_SUFFIX
    /* Opt-in host pin (see ota_url_host_allowed). Off unless built with the
       macro defined, so default builds keep accepting any https:// host. */
    if (!ota_url_host_allowed(url)) {
        ESP_LOGE(TAG, "OTA host not in allowlist");
        return ESP_ERR_INVALID_ARG;
    }
#endif
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
