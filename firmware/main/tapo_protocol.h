#ifndef TAPO_PROTOCOL_H
#define TAPO_PROTOCOL_H

#include <stdint.h>
#include "esp_err.h"

// Telemetry reading structure from the smart plug
typedef struct {
    float voltage_v;
    float current_a;
    float power_w;
    float energy_kwh;
    float temperature_c;
} tapo_telemetry_t;

/**
 * @brief Initialize local tapo driver protocol settings
 */
esp_err_t tapo_init(void);

/**
 * @brief Turn a local Tapo smart plug ON or OFF
 * 
 * @param plug_ip Local IP address of the plug on VLAN 20
 * @param turn_on true to turn ON, false to turn OFF
 * @return esp_err_t ESP_OK on success
 */
esp_err_t tapo_set_power_state(const char *plug_ip, bool turn_on);

/**
 * @brief Read power telemetry from a local Tapo smart plug
 * 
 * @param plug_ip Local IP address of the plug
 * @param out_telemetry Pointer to write telemetry results into
 * @return esp_err_t ESP_OK on success
 */
esp_err_t tapo_get_telemetry(const char *plug_ip, tapo_telemetry_t *out_telemetry);

#endif // TAPO_PROTOCOL_H
