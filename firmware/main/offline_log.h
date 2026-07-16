#ifndef OFFLINE_LOG_H
#define OFFLINE_LOG_H

#include <stdint.h>
#include "esp_err.h"

/**
 * @brief Maximum number of offline telemetry entries the ring buffer can hold.
 *
 * At 15-second polling intervals this gives ~16 minutes of offline buffering.
 * Can be increased up to 255 (limited by NVS key naming "e00"–"eFF").
 */
#define OFFLINE_LOG_MAX_ENTRIES 64

/**
 * @brief A single telemetry snapshot captured while MQTT was disconnected.
 *
 * Stored as an NVS blob in the "offlog" namespace. Kept compact (22 bytes)
 * to minimise NVS wear. Floats are stored as fixed-point integers:
 *   - watts, voltage: × 10  (0.1 resolution)
 *   - kwh:            × 1000 (0.001 resolution, mWh)
 *   - current:        × 100  (0.01 resolution)
 *   - temperature:    × 10   (0.1 resolution)
 *
 * NOTE: changing this layout requires bumping OFFLOG_FORMAT_VER in offline_log.c
 * so stale entries buffered by an older firmware are cleared on init rather than
 * mis-read into the new struct.
 */
typedef struct {
    uint32_t timestamp_s;      /**< uptime seconds when the reading was taken */
    uint32_t session_id;       /**< backend session id this reading belongs to (0 = idle/none, TD#24) */
    uint16_t watts_x10;        /**< power in deci-Watts */
    uint32_t kwh_x1000;        /**< energy in milli-kWh */
    uint16_t voltage_x10;      /**< voltage in deci-Volts */
    uint16_t current_x100;     /**< current in centi-Amps */
    int16_t  temperature_x10;  /**< temperature in deci-°C (signed for sub-zero) */
    uint8_t  plug_id;          /**< which plug this reading belongs to */
    uint8_t  status;           /**< 0 = available, 1 = occupied */
} __attribute__((packed)) offline_telemetry_entry_t;

/**
 * @brief Initialise the offline log ring buffer from NVS.
 *
 * Loads the head/tail/count metadata. Safe to call on first boot (creates
 * the namespace if it doesn't exist).
 *
 * @return ESP_OK on success
 */
esp_err_t offline_log_init(void);

/**
 * @brief Append a telemetry snapshot to the ring buffer.
 *
 * If the buffer is full, the oldest entry is silently overwritten (ring
 * behaviour). Each call writes one NVS blob + updates the metadata blob.
 *
 * @param entry Pointer to the telemetry snapshot to store
 * @return ESP_OK on success
 */
esp_err_t offline_log_append(const offline_telemetry_entry_t *entry);

/**
 * @brief Get the number of buffered entries waiting to be resynced.
 *
 * @return Entry count (0–OFFLINE_LOG_MAX_ENTRIES)
 */
uint16_t offline_log_count(void);

/**
 * @brief Read and remove the oldest entry (FIFO drain for resync).
 *
 * The entry is erased from NVS after being copied into *entry.
 *
 * @param entry Pointer to receive the oldest buffered snapshot
 * @return ESP_OK on success, ESP_ERR_NOT_FOUND if buffer is empty
 */
esp_err_t offline_log_pop(offline_telemetry_entry_t *entry);

/**
 * @brief Clear all buffered entries and reset the ring buffer.
 *
 * Called after a successful bulk resync, or if the buffer needs to be
 * invalidated (e.g., firmware upgrade).
 *
 * @return ESP_OK on success
 */
esp_err_t offline_log_clear(void);

#endif // OFFLINE_LOG_H
