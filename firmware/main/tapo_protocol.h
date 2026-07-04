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
 *   voltage_v    NOMINAL  the P110 does not report voltage (configured nominal)
 *   current_a    DERIVED  power_w / voltage_v
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

/**
 * @brief Initialize the Tapo KLAP driver with the account credentials.
 *
 * The email/password are the Tapo cloud account (same as the mobile app) and are
 * used to derive the KLAP auth hash: SHA256(SHA1(email) + SHA1(password)). Must be
 * called once before any set/get call.
 *
 * @param tapo_email       Tapo account email
 * @param tapo_password    Tapo account password
 * @param nominal_voltage  Voltage to report and use to derive current (e.g. 230.0)
 * @return ESP_OK on success
 */
esp_err_t tapo_init(const char *tapo_email, const char *tapo_password, float nominal_voltage);

/**
 * @brief Turn a local Tapo smart plug ON or OFF (KLAP set_device_info).
 *
 * @param plug_ip Local IP address of the plug
 * @param turn_on true to turn ON, false to turn OFF
 * @return ESP_OK on success
 */
esp_err_t tapo_set_power_state(const char *plug_ip, bool turn_on);

/**
 * @brief Read power telemetry + safety status from a local Tapo smart plug.
 *
 * Issues get_energy_usage (power) and get_device_info (state + safety statuses)
 * over the KLAP session and maps them into out_telemetry.
 *
 * @param plug_ip Local IP address of the plug
 * @param out_telemetry Pointer to write telemetry results into
 * @return ESP_OK on success
 */
esp_err_t tapo_get_telemetry(const char *plug_ip, tapo_telemetry_t *out_telemetry);

#endif // TAPO_PROTOCOL_H
