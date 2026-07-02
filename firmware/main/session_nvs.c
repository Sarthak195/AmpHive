#include "session_nvs.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include <string.h>

static const char *TAG = "session_nvs";

/* NVS namespace — separate from the "storage" namespace used for WiFi/config */
#define SESSION_NVS_NAMESPACE "session"

/* NVS keys (kept short — NVS key max is 15 chars) */
#define KEY_ACTIVE       "active"
#define KEY_SESSION_ID   "sess_id"
#define KEY_START_TIME   "start_ts"
#define KEY_MAX_DUR      "max_dur_s"
#define KEY_MAX_KWH      "max_kwh"
#define KEY_START_KWH    "start_kwh"

// ─── Save ────────────────────────────────────────────────────────────────────

esp_err_t session_nvs_save(const session_params_t *params)
{
    if (!params) return ESP_ERR_INVALID_ARG;

    nvs_handle_t handle;
    esp_err_t err = nvs_open(SESSION_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS namespace '%s': %s",
                 SESSION_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    /* Write all fields — order doesn't matter, commit is atomic */
    nvs_set_u8 (handle, KEY_ACTIVE,     params->active ? 1 : 0);
    nvs_set_str(handle, KEY_SESSION_ID,  params->session_id);
    nvs_set_u32(handle, KEY_START_TIME,  params->start_time_s);
    nvs_set_u32(handle, KEY_MAX_DUR,     params->max_duration_s);
    nvs_set_u32(handle, KEY_MAX_KWH,     params->max_kwh_mwh);
    nvs_set_u32(handle, KEY_START_KWH,   params->start_energy_mwh);

    err = nvs_commit(handle);
    nvs_close(handle);

    if (err == ESP_OK) {
        ESP_LOGI(TAG, "Session persisted to NVS (id=%s, max_dur=%lus, max_kwh=%lumWh)",
                 params->session_id, params->max_duration_s, params->max_kwh_mwh);
    } else {
        ESP_LOGE(TAG, "NVS commit failed: %s", esp_err_to_name(err));
    }
    return err;
}

// ─── Load ────────────────────────────────────────────────────────────────────

esp_err_t session_nvs_load(session_params_t *params)
{
    if (!params) return ESP_ERR_INVALID_ARG;

    /* Default: no active session */
    memset(params, 0, sizeof(*params));

    nvs_handle_t handle;
    esp_err_t err = nvs_open(SESSION_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        /* Namespace doesn't exist yet — first boot, no session */
        return ESP_OK;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS namespace '%s': %s",
                 SESSION_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    uint8_t active_u8 = 0;
    err = nvs_get_u8(handle, KEY_ACTIVE, &active_u8);
    if (err == ESP_ERR_NVS_NOT_FOUND || active_u8 == 0) {
        /* No active session stored */
        nvs_close(handle);
        return ESP_OK;
    }

    params->active = true;

    size_t id_size = sizeof(params->session_id);
    if (nvs_get_str(handle, KEY_SESSION_ID, params->session_id, &id_size) != ESP_OK) {
        params->session_id[0] = '\0';
    }

    nvs_get_u32(handle, KEY_START_TIME, &params->start_time_s);
    nvs_get_u32(handle, KEY_MAX_DUR,    &params->max_duration_s);
    nvs_get_u32(handle, KEY_MAX_KWH,    &params->max_kwh_mwh);
    nvs_get_u32(handle, KEY_START_KWH,  &params->start_energy_mwh);

    nvs_close(handle);

    ESP_LOGW(TAG, "Recovered active session from NVS (id=%s, max_dur=%lus)",
             params->session_id, params->max_duration_s);
    return ESP_OK;
}

// ─── Clear ───────────────────────────────────────────────────────────────────

esp_err_t session_nvs_clear(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(SESSION_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        /* If namespace doesn't exist, nothing to clear */
        if (err == ESP_ERR_NVS_NOT_FOUND) return ESP_OK;
        ESP_LOGE(TAG, "Failed to open NVS for clear: %s", esp_err_to_name(err));
        return err;
    }

    /* Set active = 0 rather than erasing the whole namespace, so the keys
       are pre-allocated for the next session_nvs_save (faster writes) */
    nvs_set_u8(handle, KEY_ACTIVE, 0);
    err = nvs_commit(handle);
    nvs_close(handle);

    ESP_LOGI(TAG, "Session cleared from NVS.");
    return err;
}

// ─── Has Active ──────────────────────────────────────────────────────────────

bool session_nvs_has_active(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(SESSION_NVS_NAMESPACE, NVS_READONLY, &handle);
    if (err != ESP_OK) return false;

    uint8_t active_u8 = 0;
    err = nvs_get_u8(handle, KEY_ACTIVE, &active_u8);
    nvs_close(handle);

    return (err == ESP_OK && active_u8 == 1);
}
