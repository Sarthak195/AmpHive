#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_system.h"
#include "esp_random.h"
#include "esp_mac.h"
#include "nvs_flash.h"
#include "nvs.h"
#include "esp_netif.h"
#include "mqtt_client.h"
#include "microlink.h"
#include "tapo_protocol.h"
#include "esp_http_server.h"
#include "session_nvs.h"
#include "offline_log.h"
#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_ota_ops.h"
#include "ota_update.h"

// ─── Configuration Variables ──────────────────────────────────────────────────
char wifi_ssid[32] = "";
char wifi_password[64] = "";
char ts_auth_key[128] = "";
char device_name[32] = "";
char gateway_id[32] = "";
char tapo_email[64] = "";
char tapo_password[64] = "";
// MQTT broker credentials (optional). Empty = connect anonymously, which the
// broker allows until the stage-2 allow_anonymous=false flip (SECURITY.md §3).
char mqtt_username[64] = "";
char mqtt_password[64] = "";

bool config_loaded = false;
static uint32_t telemetry_interval_ms = 10000; // Default 10s (10000ms)

// ── Per-plug current-cap watchdog (REC-03) ───────────────────────────────────
// Each session carries an effective per-plug current cap (amps). When the plug's
// REAL measured current exceeds it beyond a small margin for a short debounce,
// the gateway forces the relay OFF locally and raises an OVERCURRENT_CAP alarm —
// a firmware-enforced sub-default limit, distinct from the P110's own hardware
// OVERCURRENT_CUTOFF safety trip. The cap defaults to DEFAULT_PLUG_CAP_A (matches
// the backend's effective_plug_cap default) when the ON payload omits max_current_a;
// an installer may override the default via the NVS "plug_cap_a" string key.
#define DEFAULT_PLUG_CAP_A          16.0f  // P110 rating; matches backend DEFAULT_PLUG_CAP_A
#define OVERCURRENT_CAP_MARGIN_A    0.5f   // headroom above the cap before tripping
#define OVERCURRENT_CAP_DEBOUNCE    3      // consecutive over-cap polls before OFF (ride out inrush)
static float default_plug_cap_a = DEFAULT_PLUG_CAP_A;

// How often the telemetry task re-persists active sessions so elapsed session
// time survives an unclean reboot (TD#23). persist_sessions_locked() is otherwise
// only called on state-change events (start/OFF/SET_LIMITS/watchdog), so a long
// event-free session would persist elapsed_s=0 and the duration cap would restart
// from zero on reboot. This bounds the worst-case duration overrun to one interval
// while keeping NVS wear modest (same order as the offline-log cadence).
#define SESSION_PERSIST_INTERVAL_S  30

// ── Transport selection ──────────────────────────────────────────────────────
// 1: DIRECT MQTT (default since 1.3.0) — plain outbound TLS to the broker's
//    PUBLIC IP. No overlay: survives symmetric NAT/CGNAT (the device only
//    dials out), which the microlink overlay could not (see docs/SECURITY.md
//    §3 and docs/MQTT_CONTRACT.md). Confidentiality/authenticity = TLS against
//    the embedded CA; authorization = per-gateway broker creds + topic ACLs.
// 0: legacy overlay transport (microlink/WireGuard to the server's tailnet IP,
//    plaintext 1883 inside the encrypted tunnel).
#define AMPHIVE_DIRECT_MQTT 1

#if AMPHIVE_DIRECT_MQTT
// The VM's reserved static public IP (gcloud: amphive-static-ip). The broker
// cert carries this IP in its SANs; mbedTLS verifies it against the embedded CA.
#define MQTT_BROKER_URL     "mqtts://8.231.81.12:8883"
#define MQTT_USE_TLS        1
#else
// The central AmpHive server's Tailscale VPN IP
#define SERVER_VPN_IP       "100.87.241.70"
// INTERIM (2026-07-08): plaintext 1883. mqtts://8883 is verified
// working on the broker, but the ESP's custom ml_* overlay cannot carry the TLS
// handshake (transport stall — esp_tls timeout, no mbedTLS/cert error). The
// overlay is already WireGuard-encrypted, so 1883 is not a confidentiality
// regression. Restore to "mqtts://100.87.241.70:8883" once the overlay TLS
// path is fixed (see auto-memory: broker-tls-ondevice-verify-pending).
#define MQTT_BROKER_URL     "mqtt://100.87.241.70:1883"
// Must match the URI scheme above: 1 for mqtts:// (TLS), 0 for mqtt:// (plain).
// esp-mqtt refuses to init a client that has TLS verification configs set on a
// non-SSL scheme ("Client was not initialized"), so the CA cert below is only
// attached when this is 1. Set back to 1 when restoring the mqtts://8883 URL.
#define MQTT_USE_TLS        0
#endif

// Broker CA, embedded via EMBED_TXTFILES (see main/CMakeLists.txt). The
// linker appends a NUL, so it is a valid PEM C-string for esp-mqtt.
extern const uint8_t mqtt_ca_crt_start[] asm("_binary_mqtt_ca_crt_start");
// ─────────────────────────────────────────────────────────────────────────────

static const char *TAG = "amphive_gateway";

// Extract the plug id from a command topic ".../plugs/{id}/commands".
// Returns the parsed id, or -1 if the segment isn't present.
static int parse_plug_id_from_topic(const char *topic) {
    const char *p = strstr(topic, "/plugs/");
    if (!p) return -1;
    p += strlen("/plugs/");
    if (*p < '0' || *p > '9') return -1;
    return atoi(p);
}

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0
#define WIFI_FAIL_BIT      BIT1

static esp_mqtt_client_handle_t mqtt_client = NULL;
static bool mqtt_connected = false;
static int wifi_retry_count = 0;
#define MAXIMUM_RETRY  5

// ─── Per-plug state (multi-plug, TD#20) ──────────────────────────────────────
// One ESP32 gateway drives several P110s. Each plug the backend addresses (via
// amphive/gateways/{gw}/plugs/{plug_id}/commands) gets a slot here: its DB
// plug_id, LAN IP, per-plug KLAP driver context, and its own session/watchdog
// state. The gateway learns a plug's (id, local_ip) from the ON/OFF command
// payload (the backend stores plugs.local_ip and now ships it) or from NVS
// crash recovery — it never carries a static roster.
//
// `plugs_mutex` guards the slot table structure and the session fields (both
// the MQTT command task and the telemetry task touch them). Slots are only ever
// added (never freed at runtime), and the per-plug KLAP I/O runs under the tapo
// context's *own* lock, so we never hold plugs_mutex across a network call.
#define MAX_PLUGS SESSION_NVS_MAX_PLUGS

typedef struct {
    bool         in_use;
    int          plug_id;
    char         local_ip[PLUG_IP_MAX_LEN];
    tapo_plug_t *tapo;
    // session safety-watchdog state
    bool         session_active;
    char         session_id[SESSION_ID_MAX_LEN];
    uint32_t     start_time_s;
    uint32_t     max_duration_s;
    float        start_energy_kwh;
    float        max_kwh;
    float        max_current_a;          // effective per-plug current cap (amps, REC-03)
    uint8_t      cap_over_count;         // debounce: consecutive over-cap polls
    bool         unauthorized_flagged;   // rising-edge latch for UNAUTHORIZED_ON
    bool         pending_remove;         // roster dropped this plug — telemetry task reaps it
} plug_slot_t;

static plug_slot_t plugs[MAX_PLUGS];
static SemaphoreHandle_t plugs_mutex;

static inline uint32_t now_seconds(void) {
    return (uint32_t)(xTaskGetTickCount() * portTICK_PERIOD_MS / 1000);
}

// Find the slot driving plug_id, or NULL. Caller holds plugs_mutex.
static plug_slot_t *slot_find_locked(int plug_id) {
    for (int i = 0; i < MAX_PLUGS; i++) {
        if (plugs[i].in_use && plugs[i].plug_id == plug_id) return &plugs[i];
    }
    return NULL;
}

// Find or allocate the slot for plug_id, (re)binding its IP. `local_ip` is the
// plug's LAN IP from the retained roster (amphive/gateways/{gw}/config) or an
// ON/OFF command payload. Returns NULL if the table is full or no IP is known
// (empty local_ip and no existing slot for this id). Caller holds plugs_mutex.
static plug_slot_t *slot_get_locked(int plug_id, const char *local_ip) {
    const char *ip = (local_ip && local_ip[0]) ? local_ip : NULL;

    plug_slot_t *s = slot_find_locked(plug_id);
    if (s) {
        if (ip && ip[0] && strncmp(s->local_ip, ip, sizeof(s->local_ip)) != 0) {
            strncpy(s->local_ip, ip, sizeof(s->local_ip) - 1);
            s->local_ip[sizeof(s->local_ip) - 1] = '\0';
            tapo_plug_set_ip(s->tapo, s->local_ip);
        }
        s->pending_remove = false;   // named again → keep
        return s;
    }
    if (!ip || !ip[0]) {
        ESP_LOGW(TAG, "No IP for plug %d (not in payload, no provisioned target)", plug_id);
        return NULL;
    }
    // No slot for this id yet. If an idle slot already drives this IP — the
    // boot-time provisional slot (default id), or the plug previously known
    // under a different id — adopt the real id into it rather than allocating a
    // duplicate slot for the same physical plug. A mid-session slot is skipped
    // (never steal an active session's id).
    for (int i = 0; i < MAX_PLUGS; i++) {
        if (plugs[i].in_use && !plugs[i].session_active &&
            strncmp(plugs[i].local_ip, ip, sizeof(plugs[i].local_ip)) == 0) {
            ESP_LOGI(TAG, "Adopting plug id %d into slot %d (was id %d @ %s)",
                     plug_id, i, plugs[i].plug_id, ip);
            plugs[i].plug_id = plug_id;
            plugs[i].unauthorized_flagged = false;
            plugs[i].pending_remove = false;
            tapo_plug_reassign_id(plugs[i].tapo, plug_id);
            return &plugs[i];
        }
    }
    for (int i = 0; i < MAX_PLUGS; i++) {
        if (plugs[i].in_use) continue;
        tapo_plug_t *t = tapo_plug_create(plug_id, ip);
        if (!t) return NULL;
        plugs[i].plug_id = plug_id;
        strncpy(plugs[i].local_ip, ip, sizeof(plugs[i].local_ip) - 1);
        plugs[i].local_ip[sizeof(plugs[i].local_ip) - 1] = '\0';
        plugs[i].tapo = t;
        plugs[i].session_active = false;
        plugs[i].max_current_a = default_plug_cap_a;
        plugs[i].cap_over_count = 0;
        plugs[i].unauthorized_flagged = false;
        plugs[i].pending_remove = false;
        plugs[i].in_use = true;   // publish the slot last (fields are set above)
        ESP_LOGI(TAG, "Tracking plug %d @ %s (slot %d)", plug_id, ip, i);
        return &plugs[i];
    }
    ESP_LOGW(TAG, "Plug slot table full (%d); ignoring plug %d", MAX_PLUGS, plug_id);
    return NULL;
}

// Persist every active session to NVS so a crash recovers them all. Caller
// holds plugs_mutex.
static void persist_sessions_locked(void) {
    session_params_t arr[MAX_PLUGS];
    int n = 0;
    for (int i = 0; i < MAX_PLUGS; i++) {
        if (!plugs[i].in_use || !plugs[i].session_active) continue;
        session_params_t *sp = &arr[n++];
        memset(sp, 0, sizeof(*sp));
        sp->active = true;
        sp->plug_id = plugs[i].plug_id;
        strncpy(sp->local_ip, plugs[i].local_ip, sizeof(sp->local_ip) - 1);
        strncpy(sp->session_id, plugs[i].session_id, sizeof(sp->session_id) - 1);
        sp->start_time_s = plugs[i].start_time_s;
        /* Persist elapsed-so-far, not the tick-based start (which resets to 0 on
           reboot). Recovery restores start_time_s = now - elapsed_s so the
           duration watchdog keeps counting across an unclean reboot (TD#23). */
        sp->elapsed_s = now_seconds() - plugs[i].start_time_s;
        sp->max_duration_s = plugs[i].max_duration_s;
        sp->max_kwh_mwh = (uint32_t)(plugs[i].max_kwh * 1000.0f);
        sp->start_energy_mwh = (uint32_t)(plugs[i].start_energy_kwh * 1000.0f);
    }
    session_nvs_save_all(arr, n);
}

// Apply a retained plug roster (amphive/gateways/{gw}/config): the backend's
// full list of this gateway's plugs {plug_id, local_ip, max_current_a}.
// Proactively (re)builds the slot table so idle telemetry flows for every plug
// without waiting for a command, and re-IPs a plug on a DHCP change. Plugs no
// longer in the roster are FLAGGED (pending_remove); the telemetry task — the
// sole owner of per-plug KLAP I/O — frees the context at the top of its sweep,
// so we never free a handle another task holds. An active session is never
// removed; it finalises on its own and is reaped by a later roster once idle.
static void handle_plug_roster(const cJSON *root) {
    const cJSON *plugs_arr = cJSON_GetObjectItemCaseSensitive(root, "plugs");
    if (!cJSON_IsArray(plugs_arr)) {
        ESP_LOGW(TAG, "Roster has no 'plugs' array; ignoring.");
        return;
    }

    int seen[MAX_PLUGS];   // plug_ids the roster named (and we could slot)
    int seen_n = 0;

    xSemaphoreTake(plugs_mutex, portMAX_DELAY);
    const cJSON *entry;
    cJSON_ArrayForEach(entry, plugs_arr) {
        const cJSON *idj = cJSON_GetObjectItemCaseSensitive(entry, "plug_id");
        const cJSON *ipj = cJSON_GetObjectItemCaseSensitive(entry, "local_ip");
        if (!cJSON_IsNumber(idj) || !cJSON_IsString(ipj) || !ipj->valuestring) continue;
        int plug_id = (int)idj->valuedouble;
        plug_slot_t *s = slot_get_locked(plug_id, ipj->valuestring);
        if (!s) continue;   // table full / no IP — slot_get_locked already logged
        // Arm the cap watchdog from the roster when a per-plug cap is given (an
        // ON command later overrides with the session's resolved cap). Never
        // touch an active session's live cap.
        const cJSON *capj = cJSON_GetObjectItemCaseSensitive(entry, "max_current_a");
        if (!s->session_active && cJSON_IsNumber(capj) && capj->valuedouble > 0.0) {
            s->max_current_a = (float)capj->valuedouble;
        }
        if (seen_n < MAX_PLUGS) seen[seen_n++] = plug_id;
    }

    // Reap slots the roster dropped — but never an active session.
    for (int i = 0; i < MAX_PLUGS; i++) {
        if (!plugs[i].in_use || plugs[i].session_active) continue;
        bool in_roster = false;
        for (int j = 0; j < seen_n; j++) {
            if (plugs[i].plug_id == seen[j]) { in_roster = true; break; }
        }
        if (!in_roster && !plugs[i].pending_remove) {
            plugs[i].pending_remove = true;
            ESP_LOGI(TAG, "Plug %d dropped from roster — flagged for removal (slot %d)",
                     plugs[i].plug_id, i);
        }
    }
    xSemaphoreGive(plugs_mutex);
    ESP_LOGI(TAG, "Applied plug roster: %d plug(s)", seen_n);
}

// --- Forward Declarations ---
static void start_mqtt_client(void);
static void telemetry_task(void *pvParameters);
static void start_captive_portal(void);
static void resync_offline_logs(void);
#if !AMPHIVE_DIRECT_MQTT
static void on_overlay_disconnected(void);
static void microlink_task(void *pvParameters);
#endif

// ─── NVS Configuration Helpers ───────────────────────────────────────────────
static void load_config_from_nvs(void) {
    nvs_handle_t my_handle;
    esp_err_t err = nvs_open("storage", NVS_READWRITE, &my_handle);
    if (err != ESP_OK) return;

    size_t size;
    size = sizeof(wifi_ssid);
    if (nvs_get_str(my_handle, "wifi_ssid", wifi_ssid, &size) == ESP_OK) {
        config_loaded = true;
    }
    size = sizeof(wifi_password);
    nvs_get_str(my_handle, "wifi_pwd", wifi_password, &size);
    size = sizeof(ts_auth_key);
    nvs_get_str(my_handle, "ts_auth_key", ts_auth_key, &size);
    size = sizeof(device_name);
    nvs_get_str(my_handle, "device_name", device_name, &size);
    size = sizeof(gateway_id);
    nvs_get_str(my_handle, "gateway_id", gateway_id, &size);
    size = sizeof(tapo_email);
    nvs_get_str(my_handle, "tapo_email", tapo_email, &size);
    size = sizeof(tapo_password);
    nvs_get_str(my_handle, "tapo_pwd", tapo_password, &size);
    size = sizeof(mqtt_username);
    nvs_get_str(my_handle, "mqtt_user", mqtt_username, &size);
    size = sizeof(mqtt_password);
    nvs_get_str(my_handle, "mqtt_pwd", mqtt_password, &size);
    // Optional per-gateway default current cap (amps), used when an ON command
    // omits max_current_a. Stored as a decimal string; ignore malformed / non-positive.
    char cap_str[16] = "";
    size = sizeof(cap_str);
    if (nvs_get_str(my_handle, "plug_cap_a", cap_str, &size) == ESP_OK) {
        float c = atof(cap_str);
        if (c > 0.0f) default_plug_cap_a = c;
    }

    nvs_close(my_handle);

    // gateway_id is ALWAYS the device's STA MAC (lower-case, no separators) —
    // it is intrinsic to the hardware, so we derive it rather than ask the
    // installer to type it. This overrides any stored value and keeps the id
    // stable across re-provisioning. device_name (legacy/overlay-only) follows.
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(gateway_id, sizeof(gateway_id), "%02x%02x%02x%02x%02x%02x",
             mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
    if (device_name[0] == '\0') {
        // gateway_id is a 12-char MAC hex; bound the field so the compiler's
        // format-truncation check is satisfied (8 + 20 < sizeof device_name).
        snprintf(device_name, sizeof(device_name), "amphive-%.20s", gateway_id);
    }
    // The broker account username == gateway_id, so default mqtt_user to it
    // (the installer only needs to supply the per-gateway password).
    if (mqtt_username[0] == '\0') {
        strncpy(mqtt_username, gateway_id, sizeof(mqtt_username) - 1);
    }

    if(config_loaded) {
        ESP_LOGI(TAG, "Config loaded from NVS. SSID: %s | gateway_id: %s", wifi_ssid, gateway_id);
    } else {
        ESP_LOGI(TAG, "No config found in NVS. Booting into setup mode. gateway_id: %s", gateway_id);
    }
}

static void save_config_to_nvs(const char* ssid, const char* pwd, const char* auth, const char* dev_name, const char* gw_id, const char* t_email, const char* t_pwd, const char* m_user, const char* m_pwd) {
    nvs_handle_t my_handle;
    esp_err_t err = nvs_open("storage", NVS_READWRITE, &my_handle);
    if (err != ESP_OK) return;

    nvs_set_str(my_handle, "wifi_ssid", ssid);
    nvs_set_str(my_handle, "wifi_pwd", pwd);
    nvs_set_str(my_handle, "ts_auth_key", auth);
    nvs_set_str(my_handle, "device_name", dev_name);
    nvs_set_str(my_handle, "gateway_id", gw_id);
    nvs_set_str(my_handle, "tapo_email", t_email);
    nvs_set_str(my_handle, "tapo_pwd", t_pwd);
    nvs_set_str(my_handle, "mqtt_user", m_user);
    nvs_set_str(my_handle, "mqtt_pwd", m_pwd);

    nvs_commit(my_handle);
    nvs_close(my_handle);
    ESP_LOGI(TAG, "Configuration saved to NVS.");
}

// ─── Captive Portal HTTP Server ─────────────────────────────────────────────

// Per-device setup code (SECURITY.md §8.1): doubles as the WPA2 passphrase of
// the setup AP and as the token gating POST /save. Generated once (first time
// the portal runs), persisted in NVS ("setup_code"), and printed over serial so
// the installer can copy it onto the unit label. WPA2 needs >= 8 chars.
#define SETUP_CODE_LEN 10
static char setup_code[SETUP_CODE_LEN + 1] = "";

// Reboot the portal after this long with no HTTP activity. Bounds how long the
// Wi-Fi-loss fallback (SECURITY.md §8.4) leaves the AP up, and lets a
// provisioned gateway retry its STA connection once Wi-Fi comes back.
#define PORTAL_IDLE_TIMEOUT_MS (10 * 60 * 1000)
static volatile TickType_t portal_last_activity;

static void load_or_create_setup_code(void) {
    nvs_handle_t my_handle;
    if (nvs_open("storage", NVS_READWRITE, &my_handle) == ESP_OK) {
        size_t size = sizeof(setup_code);
        nvs_get_str(my_handle, "setup_code", setup_code, &size);
    } else {
        my_handle = 0;
    }
    if (strlen(setup_code) < 8) {
        // Unambiguous alphabet (no 0/O, 1/l/i) — the code gets hand-copied.
        static const char alphabet[] = "23456789abcdefghjkmnpqrstuvwxyz";
        for (int i = 0; i < SETUP_CODE_LEN; i++) {
            setup_code[i] = alphabet[esp_random() % (sizeof(alphabet) - 1)];
        }
        setup_code[SETUP_CODE_LEN] = '\0';
        if (my_handle) {
            nvs_set_str(my_handle, "setup_code", setup_code);
            nvs_commit(my_handle);
        } else {
            ESP_LOGE(TAG, "NVS open failed — setup code NOT persisted (valid this boot only)");
        }
    }
    if (my_handle) nvs_close(my_handle);
    ESP_LOGI(TAG, "==============================================");
    ESP_LOGI(TAG, "  SETUP CODE: %s", setup_code);
    ESP_LOGI(TAG, "  (WPA2 password of the setup AP AND the");
    ESP_LOGI(TAG, "   'Setup Code' form field — label the unit)");
    ESP_LOGI(TAG, "==============================================");
}

static bool setup_code_matches(const char *submitted) {
    size_t n = strlen(setup_code);
    if (n < 8 || strlen(submitted) != n) return false;
    unsigned char diff = 0;
    for (size_t i = 0; i < n; i++) {
        diff |= (unsigned char)(submitted[i] ^ setup_code[i]);
    }
    return diff == 0;
}

static const char* portal_html = \
    "<html><head><title>AmpHive Gateway Setup</title>"
    "<style>body{font-family:sans-serif;margin:40px;background:#1e1e1e;color:#fff;} input{padding:10px;margin:5px 0 20px 0;width:100%%;box-sizing:border-box;border-radius:5px;border:none;} button{padding:10px 20px;background:#00d2ff;border:none;border-radius:5px;cursor:pointer;font-weight:bold;} code{background:#333;padding:3px 8px;border-radius:4px;color:#00d2ff;}</style>"
    "</head><body><h2>AmpHive Gateway Config</h2>"
    "<p>Gateway ID (auto-detected): <code>%s</code><br>"
    "<small>Give this ID to your AmpHive operator to get the MQTT password.</small></p>"
    "<form method='POST' action='/save'>"
    "<label>Setup Code (on the unit label):</label><input name='setup_code' required>"
    "<label>WiFi SSID:</label><input name='ssid' required>"
    "<label>WiFi Password:</label><input name='pwd' type='password'>"
    "<label>Tapo Account Email:</label><input name='tapo_email' type='email' required>"
    "<label>Tapo Account Password:</label><input name='tapo_pwd' type='password' required>"
    "<label>MQTT Password:</label><input name='mqtt_pwd' type='password' required>"
    "<button type='submit'>Save &amp; Reboot</button>"
    "</form></body></html>";

static esp_err_t portal_get_handler(httpd_req_t *req) {
    portal_last_activity = xTaskGetTickCount();
    // Render with the auto-detected gateway_id embedded (load_config derives it).
    // Static: 2 KB would crowd the httpd task stack, and handlers are serialized.
    static char page[2048];
    snprintf(page, sizeof(page), portal_html, gateway_id);
    httpd_resp_send(req, page, HTTPD_RESP_USE_STRLEN);
    return ESP_OK;
}

// Decode application/x-www-form-urlencoded values in place ('%XX' and '+').
// httpd_query_key_value extracts but does not decode, so e.g. a Tapo email's
// '@' arrives as "%40" and must be decoded before use.
static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static void url_decode(char *s) {
    char *dst = s;
    for (char *src = s; *src; ) {
        if (*src == '%' && hexval(src[1]) >= 0 && hexval(src[2]) >= 0) {
            *dst++ = (char)(hexval(src[1]) * 16 + hexval(src[2]));
            src += 3;
        } else if (*src == '+') {
            *dst++ = ' '; src++;
        } else {
            *dst++ = *src++;
        }
    }
    *dst = '\0';
}

// Best-effort Wi-Fi association pre-check for the captive portal (TD#31). Before
// committing new credentials + rebooting, briefly try to associate to the
// submitted SSID/password in AP+STA mode so a wrong network name/password is
// caught at provisioning instead of only at charge time. FAIL-OPEN: returns false
// ONLY on a definite association failure; a success or an inconclusive/ambiguous
// result (timeout, can't switch mode) returns true so the check can never block
// provisioning. Note: the AP may briefly hop to the STA's channel during the test
// (single radio), dropping the installer's phone; on success we reboot right after
// anyway. The plug-reachability half of TD#31 is moot — plug IPs now come from the
// backend's retained roster, so the portal has no plug IP to ping.
static bool portal_precheck_wifi(const char *ssid, const char *pwd) {
    if (!ssid || !ssid[0]) return true;   // nothing to test → don't block

    wifi_config_t sta_cfg = {0};
    strncpy((char *)sta_cfg.sta.ssid, ssid, sizeof(sta_cfg.sta.ssid) - 1);
    strncpy((char *)sta_cfg.sta.password, pwd ? pwd : "", sizeof(sta_cfg.sta.password) - 1);

    // AP+STA so the portal AP stays up while the STA link is tested. The STA netif
    // was already created in wifi_init(), and the wifi/IP event handler is
    // registered, so CONNECTED/FAIL bits are driven as usual.
    if (esp_wifi_set_mode(WIFI_MODE_APSTA) != ESP_OK) return true;
    if (esp_wifi_set_config(WIFI_IF_STA, &sta_cfg) != ESP_OK) {
        esp_wifi_set_mode(WIFI_MODE_AP);
        return true;
    }

    wifi_retry_count = 0;
    xEventGroupClearBits(wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT);
    esp_wifi_connect();

    // The handler auto-retries up to MAXIMUM_RETRY then sets WIFI_FAIL_BIT; a
    // success sets WIFI_CONNECTED_BIT on GOT_IP. Bound the wait so a slow/ambiguous
    // case can't hang the portal handler.
    EventBits_t bits = xEventGroupWaitBits(
        wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
        pdFALSE, pdFALSE, pdMS_TO_TICKS(20000));

    // Tear the STA link down and restore AP-only so the portal stays reachable if
    // we're not about to reboot.
    esp_wifi_disconnect();
    esp_wifi_set_mode(WIFI_MODE_AP);

    if (bits & WIFI_CONNECTED_BIT) return true;    // associated + got IP
    if (bits & WIFI_FAIL_BIT)      return false;   // definite failure (e.g. wrong password)
    return true;                                   // timeout/ambiguous → fail-open
}

static esp_err_t portal_post_handler(httpd_req_t *req) {
    char buf[512];
    int ret, remaining = req->content_len;

    portal_last_activity = xTaskGetTickCount();

    if (remaining >= sizeof(buf)) {
        httpd_resp_send_500(req);
        return ESP_FAIL;
    }

    ret = httpd_req_recv(req, buf, remaining);
    if (ret <= 0) {
        if (ret == HTTPD_SOCK_ERR_TIMEOUT) {
            httpd_resp_send_408(req);
        }
        return ESP_FAIL;
    }
    buf[ret] = '\0';

    // The setup code gates /save even for clients already on the (WPA2) AP —
    // e.g. another device that joined while the installer provisions.
    char code[32] = {0};
    httpd_query_key_value(buf, "setup_code", code, sizeof(code));
    url_decode(code);
    if (!setup_code_matches(code)) {
        ESP_LOGW(TAG, "Portal /save rejected: wrong setup code");
        vTaskDelay(pdMS_TO_TICKS(1000)); // throttle brute-force attempts
        httpd_resp_set_status(req, "403 Forbidden");
        httpd_resp_send(req, "<html><body><h2>Wrong setup code.</h2>"
                             "<p>Use the code on the unit label.</p></body></html>",
                        HTTPD_RESP_USE_STRLEN);
        return ESP_OK;
    }

    // gateway_id, device_name and mqtt_user are derived from the MAC (see
    // load_config_from_nvs), so the portal no longer collects them — the
    // installer supplies only Wi-Fi, the Tapo account, and the per-gateway
    // MQTT password. Plug IPs now arrive from the backend's retained roster.
    char ssid[32] = {0}, pwd[64] = {0};
    char t_email[64] = {0}, t_pwd[64] = {0}, m_pwd[64] = {0};
    httpd_query_key_value(buf, "ssid", ssid, sizeof(ssid));
    httpd_query_key_value(buf, "pwd", pwd, sizeof(pwd));
    httpd_query_key_value(buf, "tapo_email", t_email, sizeof(t_email));
    httpd_query_key_value(buf, "tapo_pwd", t_pwd, sizeof(t_pwd));
    httpd_query_key_value(buf, "mqtt_pwd", m_pwd, sizeof(m_pwd));

    url_decode(ssid); url_decode(pwd);
    url_decode(t_email); url_decode(t_pwd); url_decode(m_pwd);

    // Verify the Wi-Fi credentials actually associate before committing +
    // rebooting (TD#31). Fail-open: only a definite association failure blocks the
    // save, so this can catch a wrong SSID/password but never prevent provisioning.
    if (!portal_precheck_wifi(ssid, pwd)) {
        ESP_LOGW(TAG, "Portal /save: could not associate to Wi-Fi '%s' — not saving.", ssid);
        httpd_resp_send(req,
            "<html><body><h2>Could not connect to that Wi-Fi network.</h2>"
            "<p>Check the network name and password, then go back and try again.</p>"
            "</body></html>", HTTPD_RESP_USE_STRLEN);
        return ESP_OK;
    }

    // mqtt_user == gateway_id (== MAC); ts_auth_key is unused in direct mode.
    save_config_to_nvs(ssid, pwd, "", device_name, gateway_id,
                       t_email, t_pwd, gateway_id, m_pwd);

    const char* resp = "<html><body><h2>Saved! Rebooting gateway...</h2></body></html>";
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);

    ESP_LOGI(TAG, "Config saved. Restarting in 2 seconds...");
    vTaskDelay(2000 / portTICK_PERIOD_MS);
    esp_restart();

    return ESP_OK;
}

static void start_captive_portal(void) {
    ESP_LOGI(TAG, "Starting Captive Portal Access Point...");

    load_or_create_setup_code();
    portal_last_activity = xTaskGetTickCount();

    esp_netif_create_default_wifi_ap();

    wifi_config_t ap_config = {
        .ap = {
            .ssid = "AmpHive_Setup",
            .ssid_len = strlen("AmpHive_Setup"),
            .channel = 1,
            .max_connection = 2,
            .authmode = WIFI_AUTH_WPA2_PSK
        },
    };
    // The per-device setup code is the WPA2 passphrase (>= 8 chars), so the
    // submitted secrets are never sent over open air (SECURITY.md §8.1).
    strncpy((char*)ap_config.ap.password, setup_code, sizeof(ap_config.ap.password) - 1);

    // Add MAC address to SSID to make it unique
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf((char*)ap_config.ap.ssid, 32, "AmpHive_Setup_%02X%02X", mac[4], mac[5]);
    ap_config.ap.ssid_len = strlen((char*)ap_config.ap.ssid);

    // AP-only (not APSTA): the STA interface stays down, so the portal is
    // reachable exclusively via the setup AP at 192.168.4.1.
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_AP));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "AP Started: %s (WPA2, password = setup code)", ap_config.ap.ssid);

    httpd_config_t config = HTTPD_DEFAULT_CONFIG();
    httpd_handle_t server = NULL;
    if (httpd_start(&server, &config) == ESP_OK) {
        httpd_uri_t uri_get = { .uri = "/", .method = HTTP_GET, .handler = portal_get_handler, .user_ctx = NULL };
        httpd_register_uri_handler(server, &uri_get);

        httpd_uri_t uri_post = { .uri = "/save", .method = HTTP_POST, .handler = portal_post_handler, .user_ctx = NULL };
        httpd_register_uri_handler(server, &uri_post);
        ESP_LOGI(TAG, "HTTP server started on 192.168.4.1");
    }
}

// ─── WiFi Event Handler ───────────────────────────────────────────────────────
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        if(config_loaded) {
            esp_wifi_connect();
        }
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        if (wifi_retry_count < MAXIMUM_RETRY) {
            esp_wifi_connect();
            wifi_retry_count++;
            ESP_LOGW(TAG, "WiFi connection dropped. Retrying...");
        } else {
            xEventGroupSetBits(wifi_event_group, WIFI_FAIL_BIT);
        }
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "WiFi connected. Local IP: " IPSTR, IP2STR(&event->ip_info.ip));
        wifi_retry_count = 0;
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

// ─── WiFi Initialization ──────────────────────────────────────────────────────
static bool wifi_init(void) {
    wifi_event_group = xEventGroupCreate();

    esp_err_t ret = nvs_flash_init();
    if (ret == ESP_ERR_NVS_NO_FREE_PAGES || ret == ESP_ERR_NVS_NEW_VERSION_FOUND) {
      ESP_ERROR_CHECK(nvs_flash_erase());
      ret = nvs_flash_init();
    }
    ESP_ERROR_CHECK(ret);
    
    load_config_from_nvs();

    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP, &wifi_event_handler, NULL));

    if (!config_loaded) {
        return false;
    }

    wifi_config_t wifi_cfg = {0};
    strncpy((char*)wifi_cfg.sta.ssid, wifi_ssid, sizeof(wifi_cfg.sta.ssid));
    strncpy((char*)wifi_cfg.sta.password, wifi_password, sizeof(wifi_cfg.sta.password));

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Connecting to local VLAN WiFi...");
    EventBits_t bits = xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT | WIFI_FAIL_BIT,
                        pdFALSE, pdFALSE, portMAX_DELAY);
                        
    if (bits & WIFI_CONNECTED_BIT) {
        return true;
    } else {
        ESP_LOGE(TAG, "Failed to connect to STA. Will fallback to Captive Portal.");
        return false;
    }
}

// ─── OTA event → alarms topic ───────────────────────────────────────────────
// Coarse OTA lifecycle events go to the alarms topic (the backend does not
// subscribe to it, so this can't be mistaken for gateway status).
static void publish_ota_event(const char *event) {
    if (!mqtt_connected || mqtt_client == NULL) return;
    char topic[128];
    char payload[96];
    snprintf(topic, sizeof(topic), "amphive/gateways/%s/alarms", gateway_id);
    snprintf(payload, sizeof(payload), "{\"event\":\"%s\"}", event);
    esp_mqtt_client_publish(mqtt_client, topic, payload, 0, 1, 0);
}

// ─── Safety alarm → alarms topic ────────────────────────────────────────────
// Fault alarms (THERMAL_CUTOFF, OVERCURRENT_CUTOFF, UNAUTHORIZED_ON, …) carry
// the plug id so the backend can attribute them; QoS 1 (best-effort delivery of
// a one-shot event). Silent when offline — the condition is still enforced
// locally regardless of the broker link.
static void publish_alarm(const char *error, int plug_id) {
    if (!mqtt_connected || mqtt_client == NULL) return;
    char topic[128];
    char payload[96];
    snprintf(topic, sizeof(topic), "amphive/gateways/%s/alarms", gateway_id);
    snprintf(payload, sizeof(payload), "{\"error\":\"%s\",\"plug_id\":%d}", error, plug_id);
    esp_mqtt_client_publish(mqtt_client, topic, payload, 0, 1, 0);
}

// ─── Firmware log → MQTT (TD#28 firmware half) ───────────────────────────────
// Forward WARN/ERROR log lines to amphive/gateways/{gw}/logs so a deployed
// gateway can be diagnosed without a serial cable. Constraints that shape this:
//  - never block the task that logged (bounded queue, zero-timeout send, drop-on-full)
//  - never publish from the vprintf hook (it can run on the MQTT task and re-enter
//    esp-mqtt); a dedicated low-priority drain task owns every publish
//  - avoid a feedback loop: only WARN/ERROR forward, and enqueues are suppressed
//    while the drain task is inside a publish call
// The broker ACL (pattern readwrite amphive/gateways/%u/#) already covers /logs.
#define LOG_FWD_LINE_MAX   200
#define LOG_FWD_QUEUE_LEN  16
static QueueHandle_t  s_log_queue = NULL;
static vprintf_like_t s_orig_vprintf = NULL;
static volatile bool  s_log_publishing = false;

// Level letter of an ESP-IDF log line, skipping a leading ANSI colour escape.
static char log_line_level(const char *s) {
    if (s[0] == '\033') {
        const char *m = strchr(s, 'm');
        if (m) s = m + 1;
    }
    return s[0];
}

static int log_forward_vprintf(const char *fmt, va_list args) {
    // Serial first — the original handler consumes `args`, so copy for our own use.
    va_list copy;
    va_copy(copy, args);
    int r = s_orig_vprintf ? s_orig_vprintf(fmt, args) : vprintf(fmt, args);

    if (s_log_queue && !s_log_publishing) {
        char line[LOG_FWD_LINE_MAX];
        int n = vsnprintf(line, sizeof(line), fmt, copy);
        if (n > 0) {
            char lvl = log_line_level(line);
            if (lvl == 'E' || lvl == 'W') {
                (void)xQueueSend(s_log_queue, line, 0);   // drop if full — never block
            }
        }
    }
    va_end(copy);
    return r;
}

static void log_forward_task(void *pvParameters) {
    char topic[128];
    snprintf(topic, sizeof(topic), "amphive/gateways/%s/logs", gateway_id);
    char line[LOG_FWD_LINE_MAX];
    while (1) {
        if (xQueueReceive(s_log_queue, line, portMAX_DELAY) != pdTRUE) continue;
        if (!mqtt_connected || mqtt_client == NULL) continue;   // discard when offline
        size_t len = strlen(line);
        while (len && (line[len - 1] == '\n' || line[len - 1] == '\r')) line[--len] = '\0';
        if (len == 0) continue;
        s_log_publishing = true;   // don't re-enqueue logs emitted during publish
        esp_mqtt_client_publish(mqtt_client, topic, line, 0, 0, 0);
        s_log_publishing = false;
    }
}

// Install the vprintf hook + start the drain task. gateway_id must already be set.
static void start_log_forwarding(void) {
    s_log_queue = xQueueCreate(LOG_FWD_QUEUE_LEN, LOG_FWD_LINE_MAX);
    if (!s_log_queue) {
        ESP_LOGE(TAG, "log forward: queue alloc failed; serial-only logging");
        return;
    }
    s_orig_vprintf = esp_log_set_vprintf(log_forward_vprintf);
    xTaskCreate(log_forward_task, "log_forward", 4096, NULL, 3, NULL);
    ESP_LOGI(TAG, "Log forwarding enabled -> amphive/gateways/%s/logs (WARN+)", gateway_id);
}

// ─── MQTT Subscriber/Event Handler ──────────────────────────────────────────
static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t event = event_data;
    
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            ESP_LOGI(TAG, "MQTT connected to server broker.");
            mqtt_connected = true;
            
            // Publish status: ONLINE (fw version included so an OTA can be
            // verified from the broker/backend side; extra keys are ignored
            // by the backend's status handler).
            char status_topic[128];
            char status_payload[96];
            snprintf(status_topic, sizeof(status_topic), "amphive/gateways/%s/status", gateway_id);
            snprintf(status_payload, sizeof(status_payload),
                     "{\"status\":\"online\",\"fw\":\"%s\"}",
                     esp_app_get_description()->version);
            esp_mqtt_client_publish(mqtt_client, status_topic, status_payload, 0, 1, 1);

            // Subscribe to incoming commands for this gateway's plugs
            char command_topic[128];
            snprintf(command_topic, sizeof(command_topic), "amphive/gateways/%s/plugs/+/commands", gateway_id);
            esp_mqtt_client_subscribe(mqtt_client, command_topic, 1);
            ESP_LOGI(TAG, "Subscribed to commands: %s", command_topic);

            // Subscribe to the retained plug roster for this gateway. Delivered
            // on subscribe (retained), it (re)builds the slot table proactively —
            // the gateway carries no plug IPs from provisioning anymore.
            char config_topic[128];
            snprintf(config_topic, sizeof(config_topic), "amphive/gateways/%s/config", gateway_id);
            esp_mqtt_client_subscribe(mqtt_client, config_topic, 1);
            ESP_LOGI(TAG, "Subscribed to roster: %s", config_topic);

            // Reaching the broker authenticated is the self-test bar for a
            // freshly-OTA'd image: cancel the bootloader rollback.
            ota_mark_valid_if_pending();

            // Drain any offline-buffered telemetry
            resync_offline_logs();
            break;
            
        case MQTT_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "MQTT disconnected from broker.");
            mqtt_connected = false;
            break;
            
        case MQTT_EVENT_DATA: {
            char topic[256] = {0};
            char data[512]  = {0};

            // Commands are small JSON objects. Anything larger than our buffers —
            // or fragmented across multiple MQTT_EVENT_DATA callbacks — is not a
            // command we understand, so drop it rather than act on a partial
            // payload. (The old code capped data at 127 bytes, which truncated a
            // command carrying a session_id and corrupted the parse.)
            if (event->total_data_len > (int)sizeof(data) - 1) {
                ESP_LOGW(TAG, "MQTT payload too large (%d bytes); ignoring.", event->total_data_len);
                break;
            }

            int topic_len = event->topic_len > (int)sizeof(topic) - 1 ? (int)sizeof(topic) - 1 : event->topic_len;
            int data_len  = event->data_len  > (int)sizeof(data)  - 1 ? (int)sizeof(data)  - 1 : event->data_len;

            memcpy(topic, event->topic, topic_len);
            memcpy(data,  event->data,  data_len);

            ESP_LOGI(TAG, "MQTT Message Received - Topic: %s, Data: %s", topic, data);

            // Parse with a real JSON parser: robust to whitespace and field
            // ordering, and immune to the truncation/corruption the old
            // strstr/sscanf scan suffered on longer payloads.
            cJSON *root = cJSON_Parse(data);
            if (!root) {
                ESP_LOGW(TAG, "Command JSON parse failed; ignoring payload.");
                break;
            }

            // Retained plug roster (amphive/gateways/{gw}/config) — its own topic,
            // distinct from the per-plug command wildcard. Handle and return.
            if (strstr(topic, "/config")) {
                handle_plug_roster(root);
                cJSON_Delete(root);
                break;
            }

            const cJSON *action_item = cJSON_GetObjectItemCaseSensitive(root, "action");
            const char *action = cJSON_IsString(action_item) ? action_item->valuestring : NULL;

            // Which plug is this command addressed to? (OTA is gateway-scoped
            // and ignores it, but it still rides a per-plug topic.)
            int cmd_plug_id = parse_plug_id_from_topic(topic);

            // Target plug IP carried in the command payload (the backend ships
            // plugs.local_ip on ON/OFF). A command for a plug already in the
            // roster resolves by plug_id regardless; an empty local_ip only
            // fails for a plug_id that has no slot yet (the retained roster is
            // the primary source of a plug's IP now — SECURITY.md §8.5).
            char cmd_ip[PLUG_IP_MAX_LEN] = {0};
            const cJSON *ipj = cJSON_GetObjectItemCaseSensitive(root, "local_ip");
            if (cJSON_IsString(ipj) && ipj->valuestring) {
                strncpy(cmd_ip, ipj->valuestring, sizeof(cmd_ip) - 1);
                cmd_ip[sizeof(cmd_ip) - 1] = '\0';
            }

            if (action && strcmp(action, "ON") == 0 && cmd_plug_id >= 0) {
                ESP_LOGI(TAG, "Command: plug %d ON.", cmd_plug_id);

                uint32_t duration = 14400;  // 4 hours default
                float    kwh_limit = 30.0f;
                const cJSON *dur = cJSON_GetObjectItemCaseSensitive(root, "max_duration_seconds");
                if (cJSON_IsNumber(dur)) duration = (uint32_t)dur->valuedouble;
                const cJSON *kwh = cJSON_GetObjectItemCaseSensitive(root, "max_kwh");
                if (cJSON_IsNumber(kwh)) kwh_limit = (float)kwh->valuedouble;
                // Effective per-plug current cap (amps, REC-03). Omitted / non-positive
                // → fall back to the gateway default so the cap watchdog stays armed.
                float cap_a = default_plug_cap_a;
                const cJSON *capj = cJSON_GetObjectItemCaseSensitive(root, "max_current_a");
                if (cJSON_IsNumber(capj) && capj->valuedouble > 0.0) cap_a = (float)capj->valuedouble;

                char sid[SESSION_ID_MAX_LEN] = {0};   // optional backend session_id
                const cJSON *sidj = cJSON_GetObjectItemCaseSensitive(root, "session_id");
                if (cJSON_IsString(sidj) && sidj->valuestring) {
                    strncpy(sid, sidj->valuestring, sizeof(sid) - 1);
                    sid[sizeof(sid) - 1] = '\0';
                }

                // Resolve (or learn) the slot + its per-plug KLAP context.
                xSemaphoreTake(plugs_mutex, portMAX_DELAY);
                plug_slot_t *s = slot_get_locked(cmd_plug_id, cmd_ip);
                tapo_plug_t *t = s ? s->tapo : NULL;
                xSemaphoreGive(plugs_mutex);

                if (!t) {
                    ESP_LOGW(TAG, "ON for plug %d dropped: no slot/IP available.", cmd_plug_id);
                } else {
                    // Read the meter BEFORE energising (captures the pre-session
                    // baseline, so the first telemetry frame doesn't bill the
                    // meter's whole standing value).
                    tapo_telemetry_t base;
                    float baseline = (tapo_plug_get_telemetry(t, &base) == ESP_OK)
                                     ? base.energy_kwh : 0.0f;

                    // Mark the session active BEFORE the relay energises: a
                    // concurrent telemetry poll must never see device_on with no
                    // session, or the unauthorized-on guard would force our own
                    // just-started session back off.
                    xSemaphoreTake(plugs_mutex, portMAX_DELAY);
                    s->session_active = true;
                    strncpy(s->session_id, sid, sizeof(s->session_id) - 1);
                    s->session_id[sizeof(s->session_id) - 1] = '\0';
                    s->start_time_s = now_seconds();
                    s->max_duration_s = duration;
                    s->max_kwh = kwh_limit;
                    s->max_current_a = cap_a;
                    s->cap_over_count = 0;
                    s->start_energy_kwh = baseline;
                    s->unauthorized_flagged = false;
                    persist_sessions_locked();
                    xSemaphoreGive(plugs_mutex);

                    if (tapo_plug_set_power(t, true) == ESP_OK) {
                        ESP_LOGI(TAG, "Plug %d session initialized. Limit: %lu s, %.3f kWh",
                                 cmd_plug_id, duration, kwh_limit);
                    } else {
                        // Roll back the claim so telemetry/watchdog don't treat a
                        // plug that never turned on as occupied.
                        xSemaphoreTake(plugs_mutex, portMAX_DELAY);
                        s->session_active = false;
                        persist_sessions_locked();
                        xSemaphoreGive(plugs_mutex);
                        ESP_LOGE(TAG, "Plug %d ON failed; session cancelled.", cmd_plug_id);
                    }
                }
            } else if (action && strcmp(action, "OFF") == 0 && cmd_plug_id >= 0) {
                ESP_LOGI(TAG, "Command: plug %d OFF.", cmd_plug_id);
                // Learn the plug if needed so OFF can actuate even after a reboot
                // (the backend re-sends OFF to idle plugs on gateway reconnect).
                xSemaphoreTake(plugs_mutex, portMAX_DELAY);
                plug_slot_t *s = slot_get_locked(cmd_plug_id, cmd_ip);
                tapo_plug_t *t = s ? s->tapo : NULL;
                if (s) {
                    s->session_active = false;
                    persist_sessions_locked();
                }
                xSemaphoreGive(plugs_mutex);
                if (t) tapo_plug_set_power(t, false);
            } else if (action && strcmp(action, "SET_INTERVAL") == 0) {
                uint32_t interval = 10000;
                const cJSON *iv = cJSON_GetObjectItemCaseSensitive(root, "interval_ms");
                if (cJSON_IsNumber(iv)) interval = (uint32_t)iv->valuedouble;
                if (interval < 500) interval = 500;
                if (interval > 60000) interval = 60000;
                telemetry_interval_ms = interval;   // gateway-wide poll cadence
                ESP_LOGI(TAG, "Telemetry interval updated to %lu ms", interval);
            } else if (action && strcmp(action, "OTA") == 0) {
                const cJSON *u = cJSON_GetObjectItemCaseSensitive(root, "url");
                const char *url = cJSON_IsString(u) ? u->valuestring : NULL;
                // Gateway-scoped: never reboot away from a charging vehicle, so
                // refuse if ANY plug on this gateway is mid-session.
                bool any_active = false;
                xSemaphoreTake(plugs_mutex, portMAX_DELAY);
                for (int i = 0; i < MAX_PLUGS; i++) {
                    if (plugs[i].in_use && plugs[i].session_active) { any_active = true; break; }
                }
                xSemaphoreGive(plugs_mutex);
                if (!url) {
                    ESP_LOGW(TAG, "OTA command missing 'url'; ignoring.");
                } else if (any_active) {
                    ESP_LOGW(TAG, "OTA refused: a charging session is active.");
                    publish_ota_event("OTA_REFUSED_SESSION_ACTIVE");
                } else {
                    esp_err_t ota_err = ota_update_start(url);
                    if (ota_err != ESP_OK) {
                        ESP_LOGW(TAG, "OTA start failed: %s", esp_err_to_name(ota_err));
                        publish_ota_event("OTA_START_FAILED");
                    }
                }
            } else if (action && strcmp(action, "SET_LIMITS") == 0 && cmd_plug_id >= 0) {
                // Update a RUNNING session's watchdog thresholds in place. Unlike
                // ON, this MUST NOT re-read the meter baseline or touch
                // start_energy_kwh / start_time_s / session_active / session_id —
                // re-baselining mid-session would corrupt billing.
                uint32_t duration = 14400;  // 4 hours default
                float    kwh_limit = 30.0f;
                const cJSON *dur = cJSON_GetObjectItemCaseSensitive(root, "max_duration_seconds");
                if (cJSON_IsNumber(dur)) duration = (uint32_t)dur->valuedouble;
                const cJSON *kwh = cJSON_GetObjectItemCaseSensitive(root, "max_kwh");
                if (cJSON_IsNumber(kwh)) kwh_limit = (float)kwh->valuedouble;

                xSemaphoreTake(plugs_mutex, portMAX_DELAY);
                plug_slot_t *s = slot_get_locked(cmd_plug_id, cmd_ip);
                if (s && s->session_active) {
                    // Thresholds only — start_energy_kwh/start_time_s/
                    // session_active/session_id are left untouched (NO re-baseline).
                    s->max_duration_s = duration;
                    s->max_kwh = kwh_limit;
                    // Current cap is optional here: only re-arm it (and reset the
                    // debounce) when the backend actually re-sends max_current_a,
                    // so a limits update that omits it leaves the running cap intact.
                    const cJSON *capj = cJSON_GetObjectItemCaseSensitive(root, "max_current_a");
                    if (cJSON_IsNumber(capj) && capj->valuedouble > 0.0) {
                        s->max_current_a = (float)capj->valuedouble;
                        s->cap_over_count = 0;
                    }
                    persist_sessions_locked();
                    xSemaphoreGive(plugs_mutex);
                    ESP_LOGI(TAG, "Plug %d limits updated: %lu s, %.3f kWh (session preserved)",
                             cmd_plug_id, duration, kwh_limit);
                } else {
                    xSemaphoreGive(plugs_mutex);
                    ESP_LOGI(TAG, "SET_LIMITS for plug %d ignored: no active session.", cmd_plug_id);
                }
            }

            cJSON_Delete(root);
            break;
        }
        default:
            break;
    }
}

// ─── MQTT Client Startup ─────────────────────────────────────────────────────
static void start_mqtt_client(void) {
    char lwt_topic[128];
    snprintf(lwt_topic, sizeof(lwt_topic), "amphive/gateways/%s/status", gateway_id);

    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = MQTT_BROKER_URL,
#if MQTT_USE_TLS
        /* TLS: verify the broker against our embedded self-signed CA. The URI
           scheme (mqtts://) selects TLS; the CA PEM authenticates the server
           (chain + IP SAN). Dates aren't validated (no clock). Only attach the
           CA on a TLS scheme — esp-mqtt rejects SSL configs on plain mqtt://. */
        .broker.verification.certificate = (const char *)mqtt_ca_crt_start,
#endif
        .session.last_will = {
            .topic = lwt_topic,
            .msg = "{\"status\":\"offline\"}",
            .qos = 1,
            .retain = true,
        },
        /* The command handler runs on the MQTT task and now drives the Tapo KLAP
           handshake (crypto + HTTP), so give it extra stack headroom. */
        .task.stack_size = 8192,
    };

    // Present broker credentials when provisioned (NVS mqtt_user/mqtt_pwd).
    // Empty = anonymous, which the broker accepts until the stage-2 flip.
    if (mqtt_username[0] != '\0') {
        mqtt_cfg.credentials.username = mqtt_username;
        mqtt_cfg.credentials.authentication.password = mqtt_password;
        ESP_LOGI(TAG, "MQTT: authenticating as '%s'", mqtt_username);
    } else {
        ESP_LOGW(TAG, "MQTT: no broker credentials in NVS - connecting anonymously");
    }

    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
}

#if !AMPHIVE_DIRECT_MQTT
// Overlay teardown hook — microlink calls this from microlink_disconnect() BEFORE
// it frees the WireGuard netif (on an ERROR-triggered reconnect). Stop + destroy
// the MQTT client here so its TCP socket/PCB is closed while the netif still
// exists; freeing the netif under a live socket is a use-after-free crash
// (LoadProhibited). Clearing mqtt_client makes microlink_task restart MQTT once
// the overlay reconnects (state -> CONNECTED/MONITORING).
static void on_overlay_disconnected(void) {
    if (mqtt_client) {
        ESP_LOGW(TAG, "Overlay down: stopping MQTT client before netif teardown");
        esp_mqtt_client_stop(mqtt_client);
        esp_mqtt_client_destroy(mqtt_client);
        mqtt_client = NULL;
        mqtt_connected = false;
    }
}
#endif // !AMPHIVE_DIRECT_MQTT

// ─── Offline Telemetry Resync ─────────────────────────────────────────────────
static void resync_offline_logs(void) {
    uint16_t count = offline_log_count();
    if (count == 0) return;

    ESP_LOGI(TAG, "Resyncing %u offline telemetry entries...", count);

    char telemetry_topic[128];
    snprintf(telemetry_topic, sizeof(telemetry_topic), "amphive/gateways/%s/telemetry", gateway_id);

    offline_telemetry_entry_t entry;
    uint16_t sent = 0;

    while (offline_log_pop(&entry) == ESP_OK) {
        /* Echo the buffered session_id (TD#24) so the backend attributes this
           reading to the exact session. Omitted when 0 (idle): with relay:false
           the backend's idle guard then drops the frame instead of billing it to
           whatever session is ACTIVE on the plug at reconnect. "relay" mirrors the
           occupied state the reading was captured in. */
        char sid_field[48] = "";
        if (entry.session_id > 0) {
            snprintf(sid_field, sizeof(sid_field), ",\"session_id\":\"%lu\"",
                     (unsigned long)entry.session_id);
        }
        char payload[320];
        snprintf(payload, sizeof(payload),
                 "{\"plug_id\":%u,\"watts\":%.1f,\"kwh\":%.4f,\"voltage\":%.1f,"
                 "\"current\":%.2f,\"relay\":%s,\"status\":\"%s\",\"offline\":true,"
                 "\"offline_ts\":%lu%s}",
                 entry.plug_id,
                 (float)entry.watts_x10 / 10.0f,
                 (float)entry.kwh_x1000 / 1000.0f,
                 (float)entry.voltage_x10 / 10.0f,
                 (float)entry.current_x100 / 100.0f,
                 entry.status ? "true" : "false",
                 entry.status ? "occupied" : "available",
                 entry.timestamp_s,
                 sid_field);

        esp_mqtt_client_publish(mqtt_client, telemetry_topic, payload, 0, 0, 0);
        sent++;

        /* Small delay between publishes to avoid flooding the broker */
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    ESP_LOGI(TAG, "Offline resync complete: %u entries published.", sent);
}

// ─── Telemetry Polling & Watchdog Safety Loop ────────────────────────────────
// Polls EVERY known plug each cycle (safety enforcement must not depend on MQTT),
// publishes per-plug telemetry under the one per-gateway topic (plug_id in the
// body), and runs each plug's watchdog + unauthorized-on guard independently.
// With several plugs at a fast (session) cadence the loop can take longer than
// one interval to sweep them all; that just stretches the effective cadence —
// safety still runs every sweep.
static void telemetry_task(void *pvParameters) {
    char telemetry_topic[128];
    snprintf(telemetry_topic, sizeof(telemetry_topic), "amphive/gateways/%s/telemetry", gateway_id);

    while (1) {
        for (int i = 0; i < MAX_PLUGS; i++) {
            /* Reap a plug the roster dropped (pending_remove). The telemetry task
               is the SOLE owner of per-plug KLAP I/O, so freeing here — at the top
               of the sweep, before any call on this slot this iteration — cannot
               race a call in flight. Active sessions are never flagged for reap. */
            xSemaphoreTake(plugs_mutex, portMAX_DELAY);
            if (plugs[i].in_use && plugs[i].pending_remove && !plugs[i].session_active) {
                tapo_plug_t *dead = plugs[i].tapo;
                plugs[i].in_use = false;
                plugs[i].pending_remove = false;
                plugs[i].tapo = NULL;
                xSemaphoreGive(plugs_mutex);
                if (dead) tapo_plug_destroy(dead);
                continue;
            }
            /* Snapshot the slot's identity under the lock; never hold plugs_mutex
               across a KLAP network call. A slot is only ever freed by the reap
               above (this same task), so `t` stays valid after we release. */
            bool in_use = plugs[i].in_use;
            tapo_plug_t *t = plugs[i].tapo;
            xSemaphoreGive(plugs_mutex);
            if (!in_use) continue;

            /* ALWAYS poll the plug — safety enforcement must not depend on MQTT */
            tapo_telemetry_t telemetry;
            if (tapo_plug_get_telemetry(t, &telemetry) != ESP_OK) continue;

            /* Decide everything under the lock (reading the live session state),
               then do the network I/O — publish, force-off, alarm — after
               releasing it. */
            int         plug_id;
            bool        sess_active;                 // pre-watchdog state (for the payload)
            char        sid[SESSION_ID_MAX_LEN];
            float       session_kwh = 0.0f;
            bool        force_off = false, unauth_off = false, unauth_alarm = false;
            const char *cutoff_alarm = NULL;

            xSemaphoreTake(plugs_mutex, portMAX_DELAY);
            plug_slot_t *s = &plugs[i];
            plug_id     = s->plug_id;
            sess_active = s->session_active;
            strncpy(sid, s->session_id, sizeof(sid) - 1);
            sid[sizeof(sid) - 1] = '\0';

            if (s->session_active) {
                /* Report SESSION energy, not the lifetime integrator. energy_kwh is
                   a monotonic meter persisted across reboots; the backend bills this
                   "kwh" as energy-consumed-this-session, so subtract the baseline
                   captured at ON (clamp against meter/NVS-restore skew). */
                session_kwh = telemetry.energy_kwh - s->start_energy_kwh;
                if (session_kwh < 0.0f) session_kwh = 0.0f;

                uint32_t elapsed_s    = now_seconds() - s->start_time_s;
                float    consumed_kwh = telemetry.energy_kwh - s->start_energy_kwh;
                if (elapsed_s >= s->max_duration_s) {
                    ESP_LOGE(TAG, "WATCHDOG plug %d: max duration (%lu s) exceeded — local OFF.", plug_id, s->max_duration_s);
                    force_off = true; s->session_active = false;
                } else if (consumed_kwh >= s->max_kwh) {
                    ESP_LOGE(TAG, "WATCHDOG plug %d: energy limit (%.3f kWh) reached — local OFF.", plug_id, s->max_kwh);
                    force_off = true; s->session_active = false;
                } else if (telemetry.overheated) {
                    ESP_LOGE(TAG, "THERMAL plug %d: overheat_status != normal — local OFF.", plug_id);
                    force_off = true; cutoff_alarm = "THERMAL_CUTOFF"; s->session_active = false;
                } else if (telemetry.overcurrent) {
                    ESP_LOGE(TAG, "OVERCURRENT plug %d: overcurrent_status != normal — local OFF.", plug_id);
                    force_off = true; cutoff_alarm = "OVERCURRENT_CUTOFF"; s->session_active = false;
                } else if (telemetry.current_a > s->max_current_a + OVERCURRENT_CAP_MARGIN_A) {
                    /* REC-03 per-plug sub-default cap on the REAL measured current.
                       Debounced over a few polls so brief inrush/measurement spikes
                       don't cut a healthy session; distinct from the plug's own
                       hardware OVERCURRENT_CUTOFF above. */
                    if (++s->cap_over_count >= OVERCURRENT_CAP_DEBOUNCE) {
                        ESP_LOGE(TAG, "OVERCURRENT CAP plug %d: %.2f A over cap %.2f A (+%.2f) — local OFF.",
                                 plug_id, telemetry.current_a, s->max_current_a, OVERCURRENT_CAP_MARGIN_A);
                        force_off = true; cutoff_alarm = "OVERCURRENT_CAP"; s->session_active = false;
                    }
                } else {
                    s->cap_over_count = 0;   // back under the cap — reset the debounce
                }
                if (force_off) persist_sessions_locked();
            } else {
                /* Unauthorized-use guard: relay physically ON with no session
                   (physical button / Tapo app / schedule / stale NVS resume). A
                   commercial charger must not deliver energy unauthorized: force
                   OFF every cycle until it stays off, and alarm once per episode
                   (rising edge, reset when the relay is confirmed off). */
                if (telemetry.device_on) {
                    unauth_off = true;
                    if (!s->unauthorized_flagged) { unauth_alarm = true; s->unauthorized_flagged = true; }
                } else {
                    s->unauthorized_flagged = false;
                }
            }
            xSemaphoreGive(plugs_mutex);

            /* Build + ship the telemetry payload (reflecting the pre-watchdog
               session state, as the single-plug loop did). "relay" is the plug's
               ACTUAL device_on, distinct from "status" (our session state). */
            char payload[320];
            snprintf(payload, sizeof(payload),
                     "{\"plug_id\":%d,\"watts\":%.1f,\"kwh\":%.4f,\"voltage\":%.1f,\"current\":%.2f,\"relay\":%s,\"status\":\"%s\",\"session_id\":\"%s\"}",
                     plug_id,
                     telemetry.power_w,
                     session_kwh,
                     telemetry.voltage_v,
                     telemetry.current_a,
                     telemetry.device_on ? "true" : "false",
                     sess_active ? "occupied" : "available",
                     sess_active ? sid : "");

            if (mqtt_connected) {
                esp_mqtt_client_publish(mqtt_client, telemetry_topic, payload, 0, 0, 0);
            } else {
                /* Offline: buffer the reading for later resync (session-relative
                   kwh, matching the online payload). */
                offline_telemetry_entry_t log_entry = {
                    .timestamp_s     = now_seconds(),
                    /* Tag the buffered reading with its session so resync attributes
                       it to the exact session, not whatever is ACTIVE on the plug at
                       reconnect (TD#24). 0 when idle / no numeric id. */
                    .session_id      = (sess_active && sid[0]) ? (uint32_t)strtoul(sid, NULL, 10) : 0,
                    .watts_x10       = (uint16_t)(telemetry.power_w * 10.0f),
                    .kwh_x1000       = (uint32_t)(session_kwh * 1000.0f),
                    .voltage_x10     = (uint16_t)(telemetry.voltage_v * 10.0f),
                    .current_x100    = (uint16_t)(telemetry.current_a * 100.0f),
                    .temperature_x10 = (int16_t)(telemetry.temperature_c * 10.0f),
                    .plug_id         = (uint8_t)plug_id,
                    .status          = sess_active ? 1 : 0,
                };
                offline_log_append(&log_entry);
            }

            /* Deferred actuation/alarms, outside the lock (a spurious extra OFF is
               always safe). */
            if (force_off || unauth_off) {
                if (unauth_off) ESP_LOGW(TAG, "UNAUTHORIZED ON plug %d: relay on with no session — forcing OFF.", plug_id);
                tapo_plug_set_power(t, false);
            }
            if (cutoff_alarm) publish_alarm(cutoff_alarm, plug_id);
            if (unauth_alarm) publish_alarm("UNAUTHORIZED_ON", plug_id);
        }

        /* Throttled re-persist so elapsed session time survives an unclean reboot
           (TD#23). Only writes when a session is active (avoids churning NVS with
           empty sets); the event-driven persists already cover start/stop. */
        static uint32_t last_session_persist_s = 0;
        uint32_t persist_now_s = now_seconds();
        if (persist_now_s - last_session_persist_s >= SESSION_PERSIST_INTERVAL_S) {
            xSemaphoreTake(plugs_mutex, portMAX_DELAY);
            bool any_active = false;
            for (int i = 0; i < MAX_PLUGS; i++) {
                if (plugs[i].in_use && plugs[i].session_active) { any_active = true; break; }
            }
            if (any_active) persist_sessions_locked();
            xSemaphoreGive(plugs_mutex);
            last_session_persist_s = persist_now_s;
        }

        vTaskDelay(pdMS_TO_TICKS(telemetry_interval_ms));
    }
}

// ─── MicroLink VPN Tunnel Task ───────────────────────────────────────────────
#if !AMPHIVE_DIRECT_MQTT
static void microlink_task(void *pvParameters) {
    ESP_LOGI(TAG, "=== Starting AmpHive MicroLink Network Client ===");

    microlink_config_t config;
    microlink_get_default_config(&config);
    config.auth_key     = ts_auth_key;
    config.device_name  = device_name;
    config.enable_derp  = true;
    config.enable_disco = true;
    config.enable_stun  = true;
    // Stop the MQTT client before the overlay tears down its netif (UAF guard).
    config.on_disconnected = on_overlay_disconnected;

    microlink_t *ml = microlink_init(&config);
    if (!ml) {
        ESP_LOGE(TAG, "MicroLink initialization failed.");
        vTaskDelete(NULL);
        return;
    }

    microlink_connect(ml);
    microlink_state_t last_state = MICROLINK_STATE_IDLE;

    while (1) {
        microlink_update(ml);
        microlink_state_t state = microlink_get_state(ml);
        
        if (state != last_state) {
            ESP_LOGI(TAG, "VPN Tunnel State: %s -> %s",
                     microlink_state_to_str(last_state),
                     microlink_state_to_str(state));
            last_state = state;

            if (state == MICROLINK_STATE_CONNECTED || state == MICROLINK_STATE_MONITORING) {
                char ip_str[16];
                microlink_vpn_ip_to_str(microlink_get_vpn_ip(ml), ip_str);

                ESP_LOGI(TAG, "=========================================");
                ESP_LOGI(TAG, "  AMPHIVE VPN OVERLAY TUNNEL ACTIVE      ");
                ESP_LOGI(TAG, "  GATEWAY PRIVATE IP: %s                ", ip_str);
                ESP_LOGI(TAG, "=========================================");

                if (mqtt_client == NULL) {
                    start_mqtt_client();
                }
            }
        }
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}
#endif // !AMPHIVE_DIRECT_MQTT

// ─── Entry Point ─────────────────────────────────────────────────────────────
void app_main(void) {
    ESP_LOGI(TAG, "AmpHive gateway fw %s (partition: %s)",
             esp_app_get_description()->version,
             esp_ota_get_running_partition()->label);
    ota_update_init(publish_ota_event);

    // 1. Initialize WiFi and attempt STA connection
    bool wifi_connected = wifi_init();

    if (!wifi_connected) {
        // Fallback: Start Captive Portal if config is missing or STA connection failed
        start_captive_portal();
        // Wait until the user submits the portal form (which triggers reboot),
        // or reboot after PORTAL_IDLE_TIMEOUT_MS with no HTTP activity: a
        // provisioned gateway then retries its STA link (recovers from
        // transient Wi-Fi loss), and an unprovisioned one re-enters the portal
        // with a fresh window.
        while (1) {
            vTaskDelay(pdMS_TO_TICKS(1000));
            if ((xTaskGetTickCount() - portal_last_activity) * portTICK_PERIOD_MS
                    >= PORTAL_IDLE_TIMEOUT_MS) {
                ESP_LOGW(TAG, "Portal idle for %d min — rebooting to retry STA",
                         PORTAL_IDLE_TIMEOUT_MS / 60000);
                esp_restart();
            }
        }
    }

    // 2. Initialize the shared Tapo account driver (KLAP). Per-plug KLAP contexts
    //    are created lazily as the gateway learns each plug (commands / recovery).
    tapo_init(tapo_email, tapo_password, 230.0f);

    // 3. Initialize offline telemetry log
    offline_log_init();

    // 4. Create the per-plug slot table mutex before any slot is touched. app_main
    //    is single-threaded here (the telemetry/MQTT tasks start below), but the
    //    slot helpers take the lock unconditionally.
    plugs_mutex = xSemaphoreCreateMutex();

    // 5. Crash recovery: restore EVERY per-plug session persisted in NVS so each
    //    plug keeps enforcing its safety watchdog after an unclean reboot. Each
    //    recovered record carries the plug's local_ip, so we re-create its KLAP
    //    context and can drive it without waiting for a fresh backend command.
    session_params_t recovered[SESSION_NVS_MAX_PLUGS];
    int recovered_count = 0;
    session_nvs_load_all(recovered, SESSION_NVS_MAX_PLUGS, &recovered_count);
    for (int i = 0; i < recovered_count; i++) {
        ESP_LOGW(TAG, "*** CRASH RECOVERY: plug %d session '%s' (max %lu s / %.3f kWh) ***",
                 recovered[i].plug_id, recovered[i].session_id,
                 recovered[i].max_duration_s, (float)recovered[i].max_kwh_mwh / 1000.0f);

        xSemaphoreTake(plugs_mutex, portMAX_DELAY);
        plug_slot_t *s = slot_get_locked(recovered[i].plug_id, recovered[i].local_ip);
        if (s) {
            s->session_active   = true;
            strncpy(s->session_id, recovered[i].session_id, sizeof(s->session_id) - 1);
            s->session_id[sizeof(s->session_id) - 1] = '\0';
            s->max_duration_s   = recovered[i].max_duration_s;
            s->max_kwh          = (float)recovered[i].max_kwh_mwh / 1000.0f;
            s->start_energy_kwh = (float)recovered[i].start_energy_mwh / 1000.0f;
            /* The current cap is not persisted in the session NVS record, so a
               recovered session re-arms at the gateway default until the next ON /
               SET_LIMITS re-supplies max_current_a. Default (never 0) so the cap
               watchdog can't false-trip on a resumed session. */
            s->max_current_a    = default_plug_cap_a;
            s->cap_over_count   = 0;
            s->unauthorized_flagged = false;
            /* Restore the duration watchdog across the reboot (TD#23). start_time_s
               is tick-based and the tick counter restarts at zero on reboot, so we
               can't persist it directly; instead we persisted elapsed-so-far and
               back-date start_time_s by it. now_seconds() is small right after boot,
               so this subtraction underflows when elapsed_s is large — that is fine:
               it's unsigned modular arithmetic and the watchdog's
               (now_seconds() - start_time_s) recovers the true elapsed exactly. If
               the session already overran its duration, elapsed_s >= max_duration_s
               and it trips on the first telemetry sweep. */
            s->start_time_s = now_seconds() - recovered[i].elapsed_s;
        }
        xSemaphoreGive(plugs_mutex);
    }

    // 5b. Idle telemetry from boot is now driven by the retained plug roster:
    //     the gateway subscribes to amphive/gateways/{gw}/config on MQTT connect
    //     and builds its slot table from it (handle_plug_roster), so telemetry
    //     flows for every plug without a captive-portal plug IP or a provisional
    //     slot. Crash-recovered sessions repopulate their own slots below.

    // 5c. Forward WARN/ERROR logs to the broker so a deployed gateway is
    //     observable without a serial cable (TD#28). gateway_id is set by now;
    //     the drain task only publishes once MQTT connects.
    start_log_forwarding();

    // 6. Start telemetry and safety watchdog loops
    //    Stack raised to 8 KB: the task now performs real KLAP crypto + HTTP each poll.
    xTaskCreate(telemetry_task, "telemetry_safety", 8192, NULL, 5, NULL);

#if AMPHIVE_DIRECT_MQTT
    // 7. Direct transport: Wi-Fi is up, so dial the public broker over TLS
    //    right away. esp-mqtt owns reconnection (backoff on the default
    //    reconnect_timeout_ms), and the Wi-Fi netif is never torn down by us,
    //    so no overlay-style teardown hook is needed.
    ESP_LOGI(TAG, "Direct MQTT transport: connecting to " MQTT_BROKER_URL);
    start_mqtt_client();
#else
    // 7. Start Tailscale connection client
    xTaskCreate(microlink_task, "microlink_vpn", 32768, NULL, 6, NULL);
#endif
}
