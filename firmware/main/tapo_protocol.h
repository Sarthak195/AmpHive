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
 *   today_energy_kwh REAL get_energy_usage.today_energy (Wh) / 1000 -- maintained ON
 *                         THE PLUG itself (calendar-day counter, resets at local
 *                         midnight on the plug's own clock); -1.0 if the field
 *                         wasn't in the response (older/other plug firmware).
 *   month_energy_kwh REAL get_energy_usage.month_energy (Wh) / 1000 -- same, but the
 *                         calendar-MONTH counter (resets far less often). Both keep
 *                         advancing regardless of whether THIS gateway is polling —
 *                         see tapo_plug_reconcile_idle_baseline() below, the
 *                         offline/unmetered-consumption detector that reads them.
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
    float today_energy_kwh;  /**< plug-side calendar-day counter (kWh); <0 = not reported */
    float month_energy_kwh;  /**< plug-side calendar-month counter (kWh); <0 = not reported */
} tapo_telemetry_t;

/*
 * Offline/unmetered-consumption reconciliation (owner-reported incident: a
 * P110 manually toggled ON then OFF while the ESP32 gateway itself was fully
 * unreachable delivers energy nobody bills, because the firmware's own
 * session-relative `kwh` integrator only advances while ITS driver is
 * actually polling). today_energy/month_energy are maintained ON THE PLUG
 * and keep counting regardless of gateway connectivity, so comparing a fresh
 * reading against the last-known "definitely idle, nothing to hide" baseline
 * exposes that gap. See tapo_plug_reconcile_idle_baseline()'s doc comment for
 * the exact decision rule (day/month cross-check to tell a real reset from
 * real consumption) and main.c's telemetry_task for how it's wired in.
 */
typedef struct {
    bool  unmetered_detected;  /**< energy advanced with no AmpHive session covering it */
    bool  reset_detected;      /**< today/month regressed (calendar rollover or plug reset) */
    float estimated_kwh;       /**< best-effort unbilled estimate (month delta preferred) */
} tapo_energy_reconcile_t;

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
 * @brief Destroy a per-plug driver context (free its KLAP session + mutex).
 *
 * Call only when the plug has been removed from the gateway's roster and no
 * other task is using the handle. In AmpHive the telemetry task — the sole
 * owner of per-plug KLAP I/O — reaps flagged slots at the top of its sweep, so
 * the handle is never in flight when it's freed. Flushes the energy meter first
 * so a plug re-added later resumes its kWh total (`wh_<plug_id>`).
 */
void tapo_plug_destroy(tapo_plug_t *plug);

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

/**
 * @brief Reconcile a plug's today/month energy counters against the
 *        last-known "idle, nothing to hide" baseline (offline/unmetered-
 *        consumption detection).
 *
 * Call once per telemetry sweep for a plug with NO active AmpHive session
 * (main.c's telemetry_task idle branch). Compares `today_kwh`/`month_kwh`
 * (from the SAME get_energy_usage response tapo_plug_get_telemetry already
 * fetched — no extra KLAP round trip) against the baseline persisted in NVS
 * (key "bl_<plug_id>", namespace "energy" — same idiom as the per-plug
 * energy meter):
 *   - A regression on either counter (bigger than rounding jitter) is a
 *     calendar rollover (today resets nightly, month resets monthly) or a
 *     full plug reset — `reset_detected`, never `unmetered_detected`.
 *   - Otherwise, the LARGER of the two deltas (month preferred when both are
 *     valid — it resets far less often, so it's the tighter signal across a
 *     multi-hour/day gateway outage) is the unbilled estimate. Reported only
 *     past a small threshold so P110 standby-draw noise never alarms.
 *
 * Idempotent/lossless across an offline gap: the baseline is advanced (and
 * flushed to NVS) only when there is nothing pending to report, OR
 * `can_report` is true (the caller is about to actually publish — pass
 * whether MQTT is connected). A detected-but-undelivered episode leaves the
 * baseline untouched, so the NEXT poll recomputes the same (or a larger)
 * delta against the same pre-gap reference instead of the report silently
 * evaporating the moment connectivity returns.
 *
 * @param plug        Per-plug driver context
 * @param today_kwh   Plug's current today_energy reading (kWh); <0 = not reported, no-op
 * @param month_kwh   Plug's current month_energy reading (kWh); <0 = not reported, no-op
 * @param can_report  Whether the caller will actually be able to publish a
 *                     report this cycle (i.e. MQTT is connected)
 * @param out         Result (always written on ESP_OK, even when nothing detected)
 * @return ESP_OK on success
 */
esp_err_t tapo_plug_reconcile_idle_baseline(tapo_plug_t *plug, float today_kwh, float month_kwh,
                                             bool can_report, tapo_energy_reconcile_t *out);

#endif // TAPO_PROTOCOL_H
