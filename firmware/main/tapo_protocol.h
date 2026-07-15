#ifndef TAPO_PROTOCOL_H
#define TAPO_PROTOCOL_H

#include <stdint.h>
#include <stdbool.h>
#include "esp_err.h"

/*
 * Telemetry reading from a local Tapo P110 smart plug over the KLAP protocol.
 *
 * What is REAL vs derived on a P110 (verified against real hardware, fw 1.1.3):
 *   power_w      REAL     get_energy_usage.current_power (milliwatts) / 1000
 *   energy_kwh   REAL     driver-side monotonic integrator (Wh accumulated from power)
 *   device_on    REAL     get_device_info.device_on
 *   overheated   REAL     get_device_info.overheat_status    != "normal"
 *   overcurrent  REAL     get_device_info.overcurrent_status != "normal"
 *   voltage_v    REAL     get_energy_usage.voltage_mv (millivolts) / 1000
 *                         (falls back to the configured nominal if not reported)
 *   current_a    REAL     get_energy_usage.current_ma (milliamps) / 1000
 *                         (falls back to derived power_w / voltage_v if not reported)
 *   temperature_c NOMINAL the P110 has no temperature sensor (use `overheated` instead)
 */
typedef struct {
    float voltage_v;
    float current_a;
    float power_w;
    float energy_kwh;
    float temperature_c;
    bool  overheated;    /**< true if the plug reports a non-normal overheat_status */
    bool  overcurrent;   /**< true if the plug reports a non-normal overcurrent_status */
    bool  device_on;     /**< current relay state as reported by the plug */
} tapo_telemetry_t;

/*
 * Multi-plug model (TD#20): one ESP32 gateway can drive several P110s. The Tapo
 * *account* (email/password → auth_hash) and the nominal voltage are shared by
 * all of them, so those stay module-global in `tapo_init`. Everything that is
 * per-connection — the KLAP handshake/session keys, the session cookie+seq, and
 * the driver-side energy integrator — lives in a per-plug `tapo_plug_t` handle,
 * so plug A's crypto/session can never be reused to act on plug B (SECURITY.md
 * §8.5). Each plug also keeps its own NVS-persisted energy meter (key
 * "wh_<plug_id>") so a mid-session reboot doesn't disarm its energy watchdog.
 */
typedef struct tapo_plug_s tapo_plug_t;

/**
 * @brief Initialize the shared Tapo account credentials (call once, before any
 *        tapo_plug_* call).
 *
 * The email/password are the Tapo cloud account (same as the mobile app) and are
 * used to derive the KLAP auth hash: SHA256(SHA1(email) + SHA1(password)). All
 * plugs on the gateway belong to this one account.
 *
 * @param tapo_email       Tapo account email
 * @param tapo_password    Tapo account password
 * @param nominal_voltage  Voltage to report and use to derive current (e.g. 230.0)
 * @return ESP_OK on success
 */
esp_err_t tapo_init(const char *tapo_email, const char *tapo_password, float nominal_voltage);

/**
 * @brief Create a per-plug driver context.
 *
 * Allocates the plug's own KLAP session state, mutex, and energy integrator
 * (restored from NVS key "wh_<plug_id>" so the meter survives reboots). The
 * handshake is performed lazily on the first set/get. `tapo_init` must have been
 * called first.
 *
 * @param plug_id   DB plug id (used to key the plug's persisted energy meter)
 * @param local_ip  Plug's LAN IP address
 * @return a handle, or NULL on allocation failure / before tapo_init
 */
tapo_plug_t *tapo_plug_create(int plug_id, const char *local_ip);

/**
 * @brief Update a plug context's LAN IP (e.g. the plug got a new DHCP lease).
 *        Invalidates the current KLAP session so the next call re-handshakes.
 */
void tapo_plug_set_ip(tapo_plug_t *plug, const char *local_ip);

/**
 * @brief Re-key a plug context to a new DB plug id.
 *
 * Adopts the backend's real plug id into a boot-time provisional slot (see
 * main.c) and re-points the NVS energy-meter key (`wh_<plug_id>`). Only call on
 * an idle (session-less) context — it re-seats the energy integrator.
 */
void tapo_plug_reassign_id(tapo_plug_t *plug, int new_plug_id);

/**
 * @brief Turn a plug ON or OFF (KLAP set_device_info).
 * @return ESP_OK on success
 */
esp_err_t tapo_plug_set_power(tapo_plug_t *plug, bool turn_on);

/**
 * @brief Read power telemetry + safety status from a plug.
 *
 * Issues get_energy_usage (power) and get_device_info (state + safety statuses)
 * over the plug's KLAP session and maps them into out_telemetry; advances the
 * plug's monotonic energy integrator (trapezoidal) and throttled NVS persist.
 * @return ESP_OK on success
 */
esp_err_t tapo_plug_get_telemetry(tapo_plug_t *plug, tapo_telemetry_t *out_telemetry);

#endif // TAPO_PROTOCOL_H
