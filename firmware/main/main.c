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

// ─── Configuration Variables ──────────────────────────────────────────────────
char wifi_ssid[32] = "";
char wifi_password[64] = "";
char ts_auth_key[128] = "";
char device_name[32] = "";
char gateway_id[32] = "";
char target_plug_ip[16] = "";
char tapo_email[64] = "";
char tapo_password[64] = "";

bool config_loaded = false;

// The central AmpHive server's Tailscale VPN IP
#define SERVER_VPN_IP       "100.64.0.1" 
#define MQTT_BROKER_URL     "mqtt://100.64.0.1:1883"
#define TARGET_PLUG_ID      1
// ─────────────────────────────────────────────────────────────────────────────

static const char *TAG = "amphive_gateway";

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
static void microlink_task(void *pvParameters);
static void start_captive_portal(void);
static void resync_offline_logs(void);

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

    nvs_close(my_handle);
    
    if(config_loaded) {
        ESP_LOGI(TAG, "Config loaded from NVS. SSID: %s", wifi_ssid);
    } else {
        ESP_LOGI(TAG, "No config found in NVS. Booting into setup mode.");
    }
}

static void save_config_to_nvs(const char* ssid, const char* pwd, const char* auth, const char* dev_name, const char* gw_id, const char* plug_ip, const char* t_email, const char* t_pwd) {
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
    char t_email[64] = {0}, t_pwd[64] = {0};
    httpd_query_key_value(buf, "ssid", ssid, sizeof(ssid));
    httpd_query_key_value(buf, "pwd", pwd, sizeof(pwd));
    httpd_query_key_value(buf, "auth", auth, sizeof(auth));
    httpd_query_key_value(buf, "dev_name", dev, sizeof(dev));
    httpd_query_key_value(buf, "gw_id", gw, sizeof(gw));
    httpd_query_key_value(buf, "plug_ip", plug, sizeof(plug));
    httpd_query_key_value(buf, "tapo_email", t_email, sizeof(t_email));
    httpd_query_key_value(buf, "tapo_pwd", t_pwd, sizeof(t_pwd));

    url_decode(ssid); url_decode(pwd); url_decode(auth); url_decode(dev);
    url_decode(gw); url_decode(plug); url_decode(t_email); url_decode(t_pwd);

    save_config_to_nvs(ssid, pwd, auth, dev, gw, plug, t_email, t_pwd);

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

// ─── MQTT Subscriber/Event Handler ──────────────────────────────────────────
static void mqtt_event_handler(void *handler_args, esp_event_base_t base,
                               int32_t event_id, void *event_data) {
    esp_mqtt_event_handle_t event = event_data;
    
    switch ((esp_mqtt_event_id_t)event_id) {
        case MQTT_EVENT_CONNECTED:
            ESP_LOGI(TAG, "MQTT connected to server broker.");
            mqtt_connected = true;
            
            // Publish status: ONLINE
            char status_topic[128];
            snprintf(status_topic, sizeof(status_topic), "amphive/gateways/%s/status", gateway_id);
            esp_mqtt_client_publish(mqtt_client, status_topic, "{\"status\":\"online\"}", 0, 1, 1);
            
            // Subscribe to incoming commands for this gateway's plugs
            char command_topic[128];
            snprintf(command_topic, sizeof(command_topic), "amphive/gateways/%s/plugs/+/commands", gateway_id);
            esp_mqtt_client_subscribe(mqtt_client, command_topic, 1);
            ESP_LOGI(TAG, "Subscribed to commands: %s", command_topic);

            // Drain any offline-buffered telemetry
            resync_offline_logs();
            break;
            
        case MQTT_EVENT_DISCONNECTED:
            ESP_LOGW(TAG, "MQTT disconnected from broker.");
            mqtt_connected = false;
            break;
            
        case MQTT_EVENT_DATA: {
            char topic[64] = {0};
            char data[128] = {0};
            
            int topic_len = event->topic_len > 63 ? 63 : event->topic_len;
            int data_len = event->data_len > 127 ? 127 : event->data_len;
            
            memcpy(topic, event->topic, topic_len);
            memcpy(data, event->data, data_len);
            
            ESP_LOGI(TAG, "MQTT Message Received - Topic: %s, Data: %s", topic, data);
            
            // Handle ON/OFF command parsing
            if (strstr(data, "\"action\":\"ON\"") || strstr(data, "\"action\": \"ON\"")) {
                ESP_LOGI(TAG, "Command: Turning Smart Plug ON.");
                
                uint32_t duration = 14400; // 4 hours
                float kwh_limit = 30.0f;
                
                char *dur_ptr = strstr(data, "\"max_duration_seconds\":");
                if (dur_ptr) {
                    sscanf(dur_ptr, "\"max_duration_seconds\":%lu", &duration);
                }
                char *kwh_ptr = strstr(data, "\"max_kwh\":");
                if (kwh_ptr) {
                    sscanf(kwh_ptr, "\"max_kwh\":%f", &kwh_limit);
                }

                // Parse optional session_id (forward-compatible with backend)
                active_session.session_id[0] = '\0';
                char *sid_ptr = strstr(data, "\"session_id\":\"");
                if (sid_ptr) {
                    sid_ptr += strlen("\"session_id\":\"");
                    char *end = strchr(sid_ptr, '"');
                    if (end) {
                        size_t len = end - sid_ptr;
                        if (len >= SESSION_ID_MAX_LEN) len = SESSION_ID_MAX_LEN - 1;
                        memcpy(active_session.session_id, sid_ptr, len);
                        active_session.session_id[len] = '\0';
                    }
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
            } else if (strstr(data, "\"action\":\"OFF\"") || strstr(data, "\"action\": \"OFF\"")) {
                ESP_LOGI(TAG, "Command: Turning Smart Plug OFF.");
                tapo_set_power_state(target_plug_ip, false);
                active_session.active = false;
                session_nvs_clear();
            }
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

    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
}

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
            char payload[256];
            snprintf(payload, sizeof(payload),
                     "{\"plug_id\":%d,\"watts\":%.1f,\"kwh\":%.4f,\"voltage\":%.1f,\"current\":%.2f,\"status\":\"%s\"}",
                     TARGET_PLUG_ID,
                     telemetry.power_w,
                     telemetry.energy_kwh,
                     telemetry.voltage_v,
                     telemetry.current_a,
                     active_session.active ? "occupied" : "available");

            if (mqtt_connected) {
                /* Online: publish telemetry normally */
                esp_mqtt_client_publish(mqtt_client, telemetry_topic, payload, 0, 0, 0);
            } else {
                /* Offline: buffer the reading for later resync */
                offline_telemetry_entry_t log_entry = {
                    .timestamp_s   = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000,
                    .watts_x10     = (uint16_t)(telemetry.power_w * 10.0f),
                    .kwh_x1000     = (uint32_t)(telemetry.energy_kwh * 1000.0f),
                    .voltage_x10   = (uint16_t)(telemetry.voltage_v * 10.0f),
                    .current_x100  = (uint16_t)(telemetry.current_a * 100.0f),
                    .temperature_x10 = (int16_t)(telemetry.temperature_c * 10.0f),
                    .plug_id       = TARGET_PLUG_ID,
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
        vTaskDelay(pdMS_TO_TICKS(15000));
    }
}

// ─── MicroLink VPN Tunnel Task ───────────────────────────────────────────────
static void microlink_task(void *pvParameters) {
    ESP_LOGI(TAG, "=== Starting AmpHive MicroLink Network Client ===");

    microlink_config_t config;
    microlink_get_default_config(&config);
    config.auth_key     = ts_auth_key;
    config.device_name  = device_name;
    config.enable_derp  = true;
    config.enable_disco = true;
    config.enable_stun  = true;

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

// ─── Entry Point ─────────────────────────────────────────────────────────────
void app_main(void) {
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

    // 6. Start Tailscale connection client
    xTaskCreate(microlink_task, "microlink_vpn", 32768, NULL, 6, NULL);
}
