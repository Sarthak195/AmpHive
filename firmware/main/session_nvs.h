#ifndef SESSION_NVS_H
#define SESSION_NVS_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#define SESSION_ID_MAX_LEN 32

/**
 * @brief Parameters for an active charging session, persisted in NVS.
 *
 * Stored in the "session" NVS namespace so a reboot/crash can recover
 * the in-flight session and continue enforcing safety limits locally.
 */
typedef struct {
    bool     active;                        /**< true while a session is running */
    char     session_id[SESSION_ID_MAX_LEN]; /**< backend session ID (may be empty) */
    uint32_t start_time_s;                  /**< tick-derived start time (seconds) */
    uint32_t max_duration_s;                /**< max allowed duration from ON cmd */
    uint32_t max_kwh_mwh;                   /**< max_kwh × 1000 (milli-Wh integer) */
    uint32_t start_energy_mwh;              /**< starting kWh × 1000 */
} session_params_t;

/**
 * @brief Persist the current session parameters to NVS.
 *
 * Called immediately after a session is started (ON command received and
 * the plug is successfully turned on).
 *
 * @param params Pointer to the session parameters to store
 * @return ESP_OK on success
 */
esp_err_t session_nvs_save(const session_params_t *params);

/**
 * @brief Load a previously persisted session from NVS.
 *
 * Called on boot (after tapo_init) to check for crash recovery.
 * If no active session is stored, params->active will be false.
 *
 * @param params Pointer to receive the loaded session parameters
 * @return ESP_OK on success (even if no active session — check params->active)
 */
esp_err_t session_nvs_load(session_params_t *params);

/**
 * @brief Clear the persisted session from NVS.
 *
 * Called on normal session stop (OFF command) and on all watchdog cutoffs
 * (duration, energy, thermal).
 *
 * @return ESP_OK on success
 */
esp_err_t session_nvs_clear(void);

/**
 * @brief Quick check whether an active session exists in NVS.
 *
 * @return true if NVS contains an active session record
 */
bool session_nvs_has_active(void);

#endif // SESSION_NVS_H
