#include "offline_log.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_log.h"
#include <string.h>
#include <stdio.h>

static const char *TAG = "offline_log";

#define OFFLOG_NVS_NAMESPACE "offlog"
#define META_KEY             "meta"

/**
 * Ring buffer metadata — persisted as a single NVS blob so head/tail/count
 * are always atomically consistent after an nvs_commit.
 */
typedef struct {
    uint16_t head;   /**< index of the next write slot */
    uint16_t tail;   /**< index of the oldest unread entry */
    uint16_t count;  /**< number of valid entries in the buffer */
} __attribute__((packed)) ring_meta_t;

/* In-memory copy of the metadata (loaded once at init, updated on every op) */
static ring_meta_t s_meta = {0};
static bool s_initialised = false;

// ─── Internal helpers ────────────────────────────────────────────────────────

/**
 * Generate the NVS key for a given ring index: "e00" .. "e63"
 */
static void entry_key(uint16_t index, char key_buf[5])
{
    snprintf(key_buf, 5, "e%02u", index % OFFLINE_LOG_MAX_ENTRIES);
}

/**
 * Persist the current ring metadata to NVS (must be called with an open handle).
 */
static esp_err_t save_meta(nvs_handle_t handle)
{
    esp_err_t err = nvs_set_blob(handle, META_KEY, &s_meta, sizeof(s_meta));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write ring meta: %s", esp_err_to_name(err));
    }
    return err;
}

// ─── Init ────────────────────────────────────────────────────────────────────

esp_err_t offline_log_init(void)
{
    nvs_handle_t handle;
    esp_err_t err = nvs_open(OFFLOG_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to open NVS namespace '%s': %s",
                 OFFLOG_NVS_NAMESPACE, esp_err_to_name(err));
        return err;
    }

    size_t meta_size = sizeof(s_meta);
    err = nvs_get_blob(handle, META_KEY, &s_meta, &meta_size);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        /* First boot — initialise empty ring */
        memset(&s_meta, 0, sizeof(s_meta));
        save_meta(handle);
        nvs_commit(handle);
        ESP_LOGI(TAG, "Offline log initialised (empty).");
    } else if (err == ESP_OK) {
        ESP_LOGI(TAG, "Offline log loaded: %u entries buffered (head=%u, tail=%u).",
                 s_meta.count, s_meta.head, s_meta.tail);
    } else {
        ESP_LOGE(TAG, "Failed to read ring meta: %s", esp_err_to_name(err));
        memset(&s_meta, 0, sizeof(s_meta));
    }

    nvs_close(handle);
    s_initialised = true;
    return ESP_OK;
}

// ─── Append ──────────────────────────────────────────────────────────────────

esp_err_t offline_log_append(const offline_telemetry_entry_t *entry)
{
    if (!entry) return ESP_ERR_INVALID_ARG;
    if (!s_initialised) {
        ESP_LOGE(TAG, "offline_log_append called before init!");
        return ESP_ERR_INVALID_STATE;
    }

    nvs_handle_t handle;
    esp_err_t err = nvs_open(OFFLOG_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;

    /* Write the entry blob at the head position */
    char key[5];
    entry_key(s_meta.head, key);
    err = nvs_set_blob(handle, key, entry, sizeof(*entry));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to write entry %s: %s", key, esp_err_to_name(err));
        nvs_close(handle);
        return err;
    }

    /* Advance head */
    s_meta.head = (s_meta.head + 1) % OFFLINE_LOG_MAX_ENTRIES;

    if (s_meta.count < OFFLINE_LOG_MAX_ENTRIES) {
        s_meta.count++;
    } else {
        /* Ring is full — oldest entry was overwritten, advance tail */
        s_meta.tail = (s_meta.tail + 1) % OFFLINE_LOG_MAX_ENTRIES;
        ESP_LOGW(TAG, "Offline log full — oldest entry overwritten.");
    }

    save_meta(handle);
    err = nvs_commit(handle);
    nvs_close(handle);

    ESP_LOGD(TAG, "Buffered offline entry (count=%u).", s_meta.count);
    return err;
}

// ─── Count ───────────────────────────────────────────────────────────────────

uint16_t offline_log_count(void)
{
    return s_meta.count;
}

// ─── Pop ─────────────────────────────────────────────────────────────────────

esp_err_t offline_log_pop(offline_telemetry_entry_t *entry)
{
    if (!entry) return ESP_ERR_INVALID_ARG;
    if (!s_initialised) return ESP_ERR_INVALID_STATE;
    if (s_meta.count == 0) return ESP_ERR_NOT_FOUND;

    nvs_handle_t handle;
    esp_err_t err = nvs_open(OFFLOG_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;

    /* Read the tail entry */
    char key[5];
    entry_key(s_meta.tail, key);
    size_t blob_size = sizeof(*entry);
    err = nvs_get_blob(handle, key, entry, &blob_size);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to read entry %s: %s", key, esp_err_to_name(err));
        nvs_close(handle);
        return err;
    }

    /* Erase the entry and advance tail */
    nvs_erase_key(handle, key);
    s_meta.tail = (s_meta.tail + 1) % OFFLINE_LOG_MAX_ENTRIES;
    s_meta.count--;

    save_meta(handle);
    err = nvs_commit(handle);
    nvs_close(handle);

    return err;
}

// ─── Clear ───────────────────────────────────────────────────────────────────

esp_err_t offline_log_clear(void)
{
    if (!s_initialised) return ESP_ERR_INVALID_STATE;

    nvs_handle_t handle;
    esp_err_t err = nvs_open(OFFLOG_NVS_NAMESPACE, NVS_READWRITE, &handle);
    if (err != ESP_OK) return err;

    /* Erase the entire namespace — faster than key-by-key removal */
    nvs_erase_all(handle);
    memset(&s_meta, 0, sizeof(s_meta));
    save_meta(handle);
    err = nvs_commit(handle);
    nvs_close(handle);

    ESP_LOGI(TAG, "Offline log cleared.");
    return err;
}
