#ifndef SESSION_NVS_H
#define SESSION_NVS_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

#define SESSION_ID_MAX_LEN 32
#define PLUG_IP_MAX_LEN    16   /* "255.255.255.255" + NUL */

/* Max concurrent per-plug sessions a single gateway persists for crash
 * recovery. main.c sizes its plug slot table to this too, so they can't
 * diverge (a gateway with more plugs than this simply won't crash-recover the
 * overflow ones — the backend session reaper still finalises them). */
#define SESSION_NVS_MAX_PLUGS 4

/**
 * @brief Parameters for one active charging session, persisted in NVS.
 *
 * Multi-plug (TD#20): a gateway can run several plugs, so crash recovery must
 * restore *each* plug's session AND the plug's `local_ip` (the device learns
 * IPs from the backend ON command, which is gone after a reboot — without the
 * IP it couldn't re-drive the plug to keep enforcing the watchdog). The whole
 * set is stored as one blob in the "session" namespace so a save is atomic.
 */
typedef struct {
    bool     active;                          /**< true while a session is running */
    int      plug_id;                         /**< DB plug id this session drives */
    char     local_ip[PLUG_IP_MAX_LEN];       /**< plug LAN IP (to re-drive on recovery) */
    char     session_id[SESSION_ID_MAX_LEN];  /**< backend session ID (may be empty) */
    uint32_t start_time_s;                    /**< tick-derived start time (seconds) */
    uint32_t elapsed_s;                       /**< seconds already elapsed as of this save
                                                   (survives reboot; start_time_s does not — TD#23) */
    uint32_t max_duration_s;                  /**< max allowed duration from ON cmd */
    uint32_t max_kwh_mwh;                      /**< max_kwh × 1000 (milli-Wh integer) */
    uint32_t start_energy_mwh;                /**< starting kWh × 1000 */
} session_params_t;

/**
 * @brief Persist the full set of active sessions to NVS (atomic replace).
 *
 * Called whenever any plug's session state changes (start / stop / watchdog
 * cutoff). Pass every currently-active session; anything not in the array is
 * dropped from NVS. `count` is clamped to SESSION_NVS_MAX_PLUGS.
 *
 * @param arr   Array of active session params (only `active` entries need be included)
 * @param count Number of entries in `arr`
 * @return ESP_OK on success
 */
esp_err_t session_nvs_save_all(const session_params_t *arr, int count);

/**
 * @brief Load all persisted active sessions from NVS (for crash recovery).
 *
 * @param arr        Caller buffer to receive the sessions
 * @param max        Capacity of `arr`
 * @param out_count  Set to the number of sessions written (0 if none)
 * @return ESP_OK on success (even if none — check *out_count)
 */
esp_err_t session_nvs_load_all(session_params_t *arr, int max, int *out_count);

#endif // SESSION_NVS_H
