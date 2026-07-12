#include "session_nvs.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "session_nvs";

/* NVS namespace — separate from the "storage" namespace used for WiFi/config */
#define SESSION_NVS_NAMESPACE "session"

/* One blob holds the whole active-session array + a count. Storing the set as a
 * single blob (rather than per-plug keys) keeps a save atomic and load trivial.
 *
 * NOTE: this blob format supersedes the pre-multi-plug single-session layout
 * (individual u8/str/u32 keys). After an OTA from that firmware the new keys
 * won't exist, so load_all returns 0 sessions — i.e. an in-flight session isn't
 * crash-recovered across the upgrade. That's safe: the firmware refuses OTA
 * while a session is active, so there is never an active session to lose here. */
#define KEY_COUNT  "count"
#define KEY_ARR    "arr"

esp_err_t session_nvs_save_all(const session_params_t *arr, int count)
{
    if (count < 0) count = 0;
    if (count > SESSION_NVS_MAX_PLUGS) count = SESSION_NVS_MAX_PLUGS;
    if (count > 0 && !arr) return ESP_ERR_INVALID_ARG;

    nvs_handle_t handle;
    esp_err_t err = nvs_open(SESSION_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS namespace '%s': %s",
                 SESSION_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    /* Write the array (only the active entries) as one blob + its count. If the
     * count is 0 we still write it (count=0) so a prior set is cleared. */
    err = nvs_set_u32(handle, KEY_COUNT, (uint32_t)count);
    if (err == ESP_OK) {
        if (count > 0) {
            err = nvs_set_blob(handle, KEY_ARR, arr, (size_t)count * sizeof(session_params_t));
        } else {
            /* No active sessions — drop any stale blob so a later load can't
             * read a mismatched (count=0, old blob) pair. Ignore NOT_FOUND. */
            esp_err_t e = nvs_erase_key(handle, KEY_ARR);
            if (e != ESP_OK && e != ESP_ERR_NVS_NOT_FOUND) err = e;
        }
    }
    if (err == ESP_OK) err = nvs_commit(handle);
    nvs_close(handle);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Persisted %d active session(s) to NVS", count);
    } else {
        ESP_LOGE(TAG, "Failed to persist sessions: %s", esp_err_to_name(err));
    }
    return err;
}

esp_err_t session_nvs_load_all(session_params_t *arr, int max, int *out_count)
{
    if (!arr || !out_count || max <= 0) return ESP_ERR_INVALID_ARG;
    *out_count = 0;

    nvs_handle_t handle;
    esp_err_t err = nvs_open(SESSION_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return ESP_OK;   /* namespace doesn't exist yet — first boot, no sessions */
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS namespace '%s': %s",
                 SESSION_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    uint32_t count = 0;
    err = nvs_get_u32(handle, KEY_COUNT, &count);
    if (err == ESP_ERR_NVS_NOT_FOUND || count == 0) {
        nvs_close(handle);
        return ESP_OK;   /* no persisted sessions (or pre-multi-plug format) */
    }
    if (count > SESSION_NVS_MAX_PLUGS) count = SESSION_NVS_MAX_PLUGS;

    /* Read the blob and sanity-check its size against the count. A mismatch
     * (struct layout changed across firmware, or a partial write) means we
     * can't trust it — fail closed to "no sessions" rather than recover garbage
     * (a stuck-on relay is worse than a missed recovery; the backend reaper
     * finalises any dangling session). */
    session_params_t tmp[SESSION_NVS_MAX_PLUGS];
    size_t expected = (size_t)count * sizeof(session_params_t);
    size_t sz = sizeof(tmp);
    err = nvs_get_blob(handle, KEY_ARR, tmp, &sz);
    nvs_close(handle);
    if (err != ESP_OK || sz != expected) {
        ESP_LOGW(TAG, "Session blob missing/mismatched (err=%s, sz=%u, expected=%u) — no recovery",
                 esp_err_to_name(err), (unsigned)sz, (unsigned)expected);
        return ESP_OK;
    }

    int n = 0;
    for (uint32_t i = 0; i < count && n < max; i++) {
        if (tmp[i].active) {
            tmp[i].session_id[SESSION_ID_MAX_LEN - 1] = '\0';
            tmp[i].local_ip[PLUG_IP_MAX_LEN - 1] = '\0';
            arr[n++] = tmp[i];
        }
    }
    *out_count = n;
    ESP_LOGW(TAG, "Recovered %d active session(s) from NVS", n);
    return ESP_OK;
}
