#include <string.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/event_groups.h"
#include "esp_wifi.h"
#include "esp_event.h"
#include "esp_log.h"
#include "esp_system.h"
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
char target_plug_ip[16] = "";
char tapo_email[64] = "";
char tapo_password[64] = "";
// MQTT broker credentials (optional). Empty = connect anonymously, which the
// broker allows until the stage-2 allow_anonymous=false flip (SECURITY.md §3).
char mqtt_username[64] = "";
char mqtt_password[64] = "";

bool config_loaded = false;
static uint32_t telemetry_interval_ms = 10000; // Default 10s (10000ms)

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
#define TARGET_PLUG_ID      1

// Broker CA, embedded via EMBED_TXTFILES (see main/CMakeLists.txt). The
// linker appends a NUL, so it is a valid PEM C-string for esp-mqtt.
extern const uint8_t mqtt_ca_crt_start[] asm("_binary_mqtt_ca_crt_start");
// ─────────────────────────────────────────────────────────────────────────────

static const char *TAG = "amphive_gateway";

// The DB plug id this gateway currently drives. The backend is the source of
// truth for plug ids (it addresses every command to
// amphive/gateways/{gw}/plugs/{plug_id}/commands), so we adopt the id from the
// commands we receive rather than hardcoding it. Until the first command
// arrives we fall back to TARGET_PLUG_ID; telemetry for an unknown id is simply
// dropped by the backend, and the id self-corrects the moment a real ON/
// SET_INTERVAL command (i.e. a session) targets this plug.
static int active_plug_id = TARGET_PLUG_ID;

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

// Active session safety watchdog state
static struct {
    bool active;
    char session_id[SESSION_ID_MAX_LEN];
    uint32_t start_time_s;
    uint32_t max_duration_s;
    float start_energy_kwh;
    float max_kwh;
} active_session = {0};

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
    size = sizeof(target_plug_ip);
    nvs_get_str(my_handle, "target_plug", target_plug_ip, &size);
    size = sizeof(tapo_email);
    nvs_get_str(my_handle, "tapo_email", tapo_email, &size);
    size = sizeof(tapo_password);
    nvs_get_str(my_handle, "tapo_pwd", tapo_password, &size);
    size = sizeof(mqtt_username);
    nvs_get_str(my_handle, "mqtt_user", mqtt_username, &size);
    size = sizeof(mqtt_password);
    nvs_get_str(my_handle, "mqtt_pwd", mqtt_password, &size);

    nvs_close(my_handle);
    
    if(config_loaded) {
        ESP_LOGI(TAG, "Config loaded from NVS. SSID: %s", wifi_ssid);
    } else {
        ESP_LOGI(TAG, "No config found in NVS. Booting into setup mode.");
    }
}

static void save_config_to_nvs(const char* ssid, const char* pwd, const char* auth, const char* dev_name, const char* gw_id, const char* plug_ip, const char* t_email, const char* t_pwd, const char* m_user, const char* m_pwd) {
    nvs_handle_t my_handle;
    esp_err_t err = nvs_open("storage", NVS_READWRITE, &my_handle);
    if (err != ESP_OK) return;

    nvs_set_str(my_handle, "wifi_ssid", ssid);
    nvs_set_str(my_handle, "wifi_pwd", pwd);
    nvs_set_str(my_handle, "ts_auth_key", auth);
    nvs_set_str(my_handle, "device_name", dev_name);
    nvs_set_str(my_handle, "gateway_id", gw_id);
    nvs_set_str(my_handle, "target_plug", plug_ip);
    nvs_set_str(my_handle, "tapo_email", t_email);
    nvs_set_str(my_handle, "tapo_pwd", t_pwd);
    nvs_set_str(my_handle, "mqtt_user", m_user);
    nvs_set_str(my_handle, "mqtt_pwd", m_pwd);

    nvs_commit(my_handle);
    nvs_close(my_handle);
    ESP_LOGI(TAG, "Configuration saved to NVS.");
}

// ─── Captive Portal HTTP Server ─────────────────────────────────────────────

static const char* portal_html = \
    "<html><head><title>AmpHive Gateway Setup</title>"
    "<style>body{font-family:sans-serif;margin:40px;background:#1e1e1e;color:#fff;} input{padding:10px;margin:5px 0 20px 0;width:100%%;border-radius:5px;border:none;} button{padding:10px 20px;background:#00d2ff;border:none;border-radius:5px;cursor:pointer;font-weight:bold;}</style>"
    "</head><body><h2>AmpHive Gateway Config</h2>"
    "<form method='POST' action='/save'>"
    "<label>WiFi SSID:</label><input name='ssid' required>"
    "<label>WiFi Password:</label><input name='pwd' type='password'>"
    "<label>Headscale Auth Key (mkey:...):</label><input name='auth' required>"
    "<label>Device Name:</label><input name='dev_name' required>"
    "<label>Gateway MAC/ID:</label><input name='gw_id' required>"
    "<label>Target Plug IP:</label><input name='plug_ip' required>"
    "<label>Tapo Account Email:</label><input name='tapo_email' type='email' required>"
    "<label>Tapo Account Password:</label><input name='tapo_pwd' type='password' required>"
    "<label>MQTT Username (optional):</label><input name='mqtt_user'>"
    "<label>MQTT Password (optional):</label><input name='mqtt_pwd' type='password'>"
    "<button type='submit'>Save & Reboot</button>"
    "</form></body></html>";

static esp_err_t portal_get_handler(httpd_req_t *req) {
    httpd_resp_send(req, portal_html, HTTPD_RESP_USE_STRLEN);
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

static esp_err_t portal_post_handler(httpd_req_t *req) {
    char buf[512];
    int ret, remaining = req->content_len;

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

    char ssid[32] = {0}, pwd[64] = {0}, auth[128] = {0}, dev[32] = {0}, gw[32] = {0}, plug[16] = {0};
    char t_email[64] = {0}, t_pwd[64] = {0}, m_user[64] = {0}, m_pwd[64] = {0};
    httpd_query_key_value(buf, "ssid", ssid, sizeof(ssid));
    httpd_query_key_value(buf, "pwd", pwd, sizeof(pwd));
    httpd_query_key_value(buf, "auth", auth, sizeof(auth));
    httpd_query_key_value(buf, "dev_name", dev, sizeof(dev));
    httpd_query_key_value(buf, "gw_id", gw, sizeof(gw));
    httpd_query_key_value(buf, "plug_ip", plug, sizeof(plug));
    httpd_query_key_value(buf, "tapo_email", t_email, sizeof(t_email));
    httpd_query_key_value(buf, "tapo_pwd", t_pwd, sizeof(t_pwd));
    httpd_query_key_value(buf, "mqtt_user", m_user, sizeof(m_user));
    httpd_query_key_value(buf, "mqtt_pwd", m_pwd, sizeof(m_pwd));

    url_decode(ssid); url_decode(pwd); url_decode(auth); url_decode(dev);
    url_decode(gw); url_decode(plug); url_decode(t_email); url_decode(t_pwd);
    url_decode(m_user); url_decode(m_pwd);

    save_config_to_nvs(ssid, pwd, auth, dev, gw, plug, t_email, t_pwd, m_user, m_pwd);

    const char* resp = "<html><body><h2>Saved! Rebooting gateway...</h2></body></html>";
    httpd_resp_send(req, resp, HTTPD_RESP_USE_STRLEN);

    ESP_LOGI(TAG, "Config saved. Restarting in 2 seconds...");
    vTaskDelay(2000 / portTICK_PERIOD_MS);
    esp_restart();

    return ESP_OK;
}

static void start_captive_portal(void) {
    ESP_LOGI(TAG, "Starting Captive Portal Access Point...");
    
    esp_netif_create_default_wifi_ap();
    
    wifi_config_t ap_config = {
        .ap = {
            .ssid = "AmpHive_Setup",
            .ssid_len = strlen("AmpHive_Setup"),
            .channel = 1,
            .password = "",
            .max_connection = 4,
            .authmode = WIFI_AUTH_OPEN
        },
    };
    
    // Add MAC address to SSID to make it unique
    uint8_t mac[6];
    esp_read_mac(mac, ESP_MAC_WIFI_SOFTAP);
    snprintf((char*)ap_config.ap.ssid, 32, "AmpHive_Setup_%02X%02X", mac[4], mac[5]);
    ap_config.ap.ssid_len = strlen((char*)ap_config.ap.ssid);

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_APSTA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_AP, &ap_config));
    ESP_ERROR_CHECK(esp_wifi_start());
    
    ESP_LOGI(TAG, "AP Started: %s", ap_config.ap.ssid);

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
            const cJSON *action_item = cJSON_GetObjectItemCaseSensitive(root, "action");
            const char *action = cJSON_IsString(action_item) ? action_item->valuestring : NULL;

            // Adopt the plug id this command is addressed to, so telemetry we
            // publish is attributed to the same DB plug the backend is billing.
            // Gateway-scoped actions (OTA) are excluded: they ride the same
            // per-plug topic but say nothing about which plug we drive.
            int cmd_plug_id = parse_plug_id_from_topic(topic);
            if (cmd_plug_id >= 0 && (!action || strcmp(action, "OTA") != 0)) {
                active_plug_id = cmd_plug_id;
            }

            if (action && strcmp(action, "ON") == 0) {
                ESP_LOGI(TAG, "Command: Turning Smart Plug ON.");

                uint32_t duration = 14400;  // 4 hours default
                float    kwh_limit = 30.0f;
                const cJSON *dur = cJSON_GetObjectItemCaseSensitive(root, "max_duration_seconds");
                if (cJSON_IsNumber(dur)) duration = (uint32_t)dur->valuedouble;
                const cJSON *kwh = cJSON_GetObjectItemCaseSensitive(root, "max_kwh");
                if (cJSON_IsNumber(kwh)) kwh_limit = (float)kwh->valuedouble;

                // Optional backend session_id
                active_session.session_id[0] = '\0';
                const cJSON *sid = cJSON_GetObjectItemCaseSensitive(root, "session_id");
                if (cJSON_IsString(sid) && sid->valuestring) {
                    strncpy(active_session.session_id, sid->valuestring, SESSION_ID_MAX_LEN - 1);
                    active_session.session_id[SESSION_ID_MAX_LEN - 1] = '\0';
                }

                // Call local Tapo Driver
                if (tapo_set_power_state(target_plug_ip, true) == ESP_OK) {
                    active_session.active = true;
                    active_session.start_time_s = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000;
                    active_session.max_duration_s = duration;
                    active_session.max_kwh = kwh_limit;

                    tapo_telemetry_t telemetry;
                    if (tapo_get_telemetry(target_plug_ip, &telemetry) == ESP_OK) {
                        active_session.start_energy_kwh = telemetry.energy_kwh;
                    } else {
                        active_session.start_energy_kwh = 0.0f;
                    }

                    ESP_LOGI(TAG, "Session initialized. Limit: %lu s, %f kWh", duration, kwh_limit);

                    // Persist session to NVS for crash recovery
                    session_params_t nvs_params = {
                        .active = true,
                        .start_time_s = active_session.start_time_s,
                        .max_duration_s = active_session.max_duration_s,
                        .max_kwh_mwh = (uint32_t)(active_session.max_kwh * 1000.0f),
                        .start_energy_mwh = (uint32_t)(active_session.start_energy_kwh * 1000.0f),
                    };
                    strncpy(nvs_params.session_id, active_session.session_id, SESSION_ID_MAX_LEN);
                    session_nvs_save(&nvs_params);
                }
            } else if (action && strcmp(action, "OFF") == 0) {
                ESP_LOGI(TAG, "Command: Turning Smart Plug OFF.");
                tapo_set_power_state(target_plug_ip, false);
                active_session.active = false;
                session_nvs_clear();
            } else if (action && strcmp(action, "SET_INTERVAL") == 0) {
                uint32_t interval = 10000;
                const cJSON *iv = cJSON_GetObjectItemCaseSensitive(root, "interval_ms");
                if (cJSON_IsNumber(iv)) interval = (uint32_t)iv->valuedouble;
                if (interval < 500) interval = 500;
                if (interval > 60000) interval = 60000;
                telemetry_interval_ms = interval;
                ESP_LOGI(TAG, "Telemetry interval updated to %lu ms", interval);
            } else if (action && strcmp(action, "OTA") == 0) {
                const cJSON *u = cJSON_GetObjectItemCaseSensitive(root, "url");
                const char *url = cJSON_IsString(u) ? u->valuestring : NULL;
                if (!url) {
                    ESP_LOGW(TAG, "OTA command missing 'url'; ignoring.");
                } else if (active_session.active) {
                    // Never reboot away from a charging vehicle: the OTA
                    // restart would drop the relay/watchdog mid-session.
                    ESP_LOGW(TAG, "OTA refused: charging session active.");
                    publish_ota_event("OTA_REFUSED_SESSION_ACTIVE");
                } else {
                    esp_err_t ota_err = ota_update_start(url);
                    if (ota_err != ESP_OK) {
                        ESP_LOGW(TAG, "OTA start failed: %s", esp_err_to_name(ota_err));
                        publish_ota_event("OTA_START_FAILED");
                    }
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
        char payload[320];
        snprintf(payload, sizeof(payload),
                 "{\"plug_id\":%u,\"watts\":%.1f,\"kwh\":%.4f,\"voltage\":%.1f,"
                 "\"current\":%.2f,\"status\":\"%s\",\"offline\":true,"
                 "\"offline_ts\":%lu}",
                 entry.plug_id,
                 (float)entry.watts_x10 / 10.0f,
                 (float)entry.kwh_x1000 / 1000.0f,
                 (float)entry.voltage_x10 / 10.0f,
                 (float)entry.current_x100 / 100.0f,
                 entry.status ? "occupied" : "available",
                 entry.timestamp_s);

        esp_mqtt_client_publish(mqtt_client, telemetry_topic, payload, 0, 0, 0);
        sent++;

        /* Small delay between publishes to avoid flooding the broker */
        vTaskDelay(pdMS_TO_TICKS(50));
    }

    ESP_LOGI(TAG, "Offline resync complete: %u entries published.", sent);
}

// ─── Telemetry Polling & Watchdog Safety Loop ────────────────────────────────
static void telemetry_task(void *pvParameters) {
    tapo_telemetry_t telemetry;
    char telemetry_topic[128];
    snprintf(telemetry_topic, sizeof(telemetry_topic), "amphive/gateways/%s/telemetry", gateway_id);

    while (1) {
        /* ALWAYS poll the plug — safety enforcement must not depend on MQTT */
        esp_err_t ret = tapo_get_telemetry(target_plug_ip, &telemetry);
        if (ret == ESP_OK) {
            /* Report SESSION energy, not the lifetime integrator. telemetry.energy_kwh
               is a monotonic meter persisted across reboots (never reset); the backend
               bills this "kwh" field as energy-consumed-this-session (COINS_PER_KWH),
               and the MQTT contract / TelemetryStore define it that way. Publishing the
               raw meter re-bills the plug's whole charging history every session.
               Subtract the session baseline (same value the watchdog uses); clamp against
               meter/NVS-restore skew; idle (no session) reports 0. */
            float session_kwh = 0.0f;
            if (active_session.active) {
                session_kwh = telemetry.energy_kwh - active_session.start_energy_kwh;
                if (session_kwh < 0.0f) session_kwh = 0.0f;
            }

            /* Echo the backend session_id (empty when idle) so the backend can
               attribute this reading to the exact session, not just the plug. */
            char payload[256];
            snprintf(payload, sizeof(payload),
                     "{\"plug_id\":%d,\"watts\":%.1f,\"kwh\":%.4f,\"voltage\":%.1f,\"current\":%.2f,\"status\":\"%s\",\"session_id\":\"%s\"}",
                     active_plug_id,
                     telemetry.power_w,
                     session_kwh,
                     telemetry.voltage_v,
                     telemetry.current_a,
                     active_session.active ? "occupied" : "available",
                     active_session.active ? active_session.session_id : "");

            if (mqtt_connected) {
                /* Online: publish telemetry normally */
                esp_mqtt_client_publish(mqtt_client, telemetry_topic, payload, 0, 0, 0);
            } else {
                /* Offline: buffer the reading for later resync */
                offline_telemetry_entry_t log_entry = {
                    .timestamp_s   = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000,
                    .watts_x10     = (uint16_t)(telemetry.power_w * 10.0f),
                    /* session-relative energy, matching the online payload above */
                    .kwh_x1000     = (uint32_t)(session_kwh * 1000.0f),
                    .voltage_x10   = (uint16_t)(telemetry.voltage_v * 10.0f),
                    .current_x100  = (uint16_t)(telemetry.current_a * 100.0f),
                    .temperature_x10 = (int16_t)(telemetry.temperature_c * 10.0f),
                    .plug_id       = (uint8_t)active_plug_id,
                    .status        = active_session.active ? 1 : 0,
                };
                offline_log_append(&log_entry);
            }

            /* Check Session Safety Limits (Watchdog) — runs regardless of MQTT */
            if (active_session.active) {
                uint32_t current_time_s = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000;
                uint32_t elapsed_s = current_time_s - active_session.start_time_s;
                float consumed_kwh = telemetry.energy_kwh - active_session.start_energy_kwh;

                if (elapsed_s >= active_session.max_duration_s) {
                    ESP_LOGE(TAG, "WATCHDOG: Maximum session duration (%lu s) exceeded. Shutting down plug locally!", active_session.max_duration_s);
                    tapo_set_power_state(target_plug_ip, false);
                    active_session.active = false;
                    session_nvs_clear();
                }
                else if (consumed_kwh >= active_session.max_kwh) {
                    ESP_LOGE(TAG, "WATCHDOG: Session energy consumption limit (%f kWh) reached. Shutting down plug locally!", active_session.max_kwh);
                    tapo_set_power_state(target_plug_ip, false);
                    active_session.active = false;
                    session_nvs_clear();
                }
                else if (telemetry.overheated) {
                    ESP_LOGE(TAG, "THERMAL ALARM: Plug reports overheat_status != normal. Shutting down plug locally!");
                    tapo_set_power_state(target_plug_ip, false);
                    active_session.active = false;
                    session_nvs_clear();

                    if (mqtt_connected) {
                        char alarm_topic[128];
                        snprintf(alarm_topic, sizeof(alarm_topic), "amphive/gateways/%s/alarms", gateway_id);
                        esp_mqtt_client_publish(mqtt_client, alarm_topic, "{\"error\":\"THERMAL_CUTOFF\"}", 0, 1, 0);
                    }
                }
                else if (telemetry.overcurrent) {
                    ESP_LOGE(TAG, "OVERCURRENT ALARM: Plug reports overcurrent_status != normal. Shutting down plug locally!");
                    tapo_set_power_state(target_plug_ip, false);
                    active_session.active = false;
                    session_nvs_clear();

                    if (mqtt_connected) {
                        char alarm_topic[128];
                        snprintf(alarm_topic, sizeof(alarm_topic), "amphive/gateways/%s/alarms", gateway_id);
                        esp_mqtt_client_publish(mqtt_client, alarm_topic, "{\"error\":\"OVERCURRENT_CUTOFF\"}", 0, 1, 0);
                    }
                }
            }
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
        // Wait here infinitely until the user submits the portal form (which triggers reboot)
        while(1) {
            vTaskDelay(pdMS_TO_TICKS(1000));
        }
    }

    // 2. Initialize local smart plug driver (Tapo KLAP) with account credentials
    tapo_init(tapo_email, tapo_password, 230.0f);

    // 3. Initialize offline telemetry log
    offline_log_init();

    // 4. Check for crash-recovered session in NVS
    session_params_t recovered;
    session_nvs_load(&recovered);
    if (recovered.active) {
        ESP_LOGW(TAG, "*** CRASH RECOVERY: Restoring active session from NVS ***");
        ESP_LOGW(TAG, "    Session ID : %s", recovered.session_id);
        ESP_LOGW(TAG, "    Max dur    : %lu s", recovered.max_duration_s);
        ESP_LOGW(TAG, "    Max kWh    : %.3f", (float)recovered.max_kwh_mwh / 1000.0f);

        active_session.active = true;
        strncpy(active_session.session_id, recovered.session_id, SESSION_ID_MAX_LEN);
        active_session.start_time_s      = recovered.start_time_s;
        active_session.max_duration_s     = recovered.max_duration_s;
        active_session.max_kwh            = (float)recovered.max_kwh_mwh / 1000.0f;
        active_session.start_energy_kwh   = (float)recovered.start_energy_mwh / 1000.0f;

        /* Note: start_time_s was tick-based and the tick counter restarted
           at zero on reboot.  We recalibrate by treating the current tick
           as the new start and reducing max_duration by whatever time
           *can* be inferred later once telemetry is flowing.  For safety,
           keep the original limits — worst case the session runs a bit
           longer than intended, but the energy limit still triggers. */
        active_session.start_time_s = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000;
    }

    // 5. Start telemetry and safety watchdog loops
    //    Stack raised to 8 KB: the task now performs real KLAP crypto + HTTP each poll.
    xTaskCreate(telemetry_task, "telemetry_safety", 8192, NULL, 5, NULL);

#if AMPHIVE_DIRECT_MQTT
    // 6. Direct transport: Wi-Fi is up, so dial the public broker over TLS
    //    right away. esp-mqtt owns reconnection (backoff on the default
    //    reconnect_timeout_ms), and the Wi-Fi netif is never torn down by us,
    //    so no overlay-style teardown hook is needed.
    ESP_LOGI(TAG, "Direct MQTT transport: connecting to " MQTT_BROKER_URL);
    start_mqtt_client();
#else
    // 6. Start Tailscale connection client
    xTaskCreate(microlink_task, "microlink_vpn", 32768, NULL, 6, NULL);
#endif
}
