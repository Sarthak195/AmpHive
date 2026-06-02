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
#include "nvs_flash.h"
#include "esp_netif.h"
#include "mqtt_client.h"
#include "microlink.h"
#include "tapo_protocol.h"

// ─── Configuration ────────────────────────────────────────────────────────────
#define WIFI_SSID           "AmpHive_VLAN20_IoT"
#define WIFI_PASSWORD       "SecureIoTPassword"
#define TS_AUTH_KEY         "mkey:amphive-headscale-preauth-key"
#define DEVICE_NAME         "amphive_gateway_01"
#define GATEWAY_ID          "00_11_22_33_aa_bb"

// The central AmpHive server's Tailscale VPN IP
#define SERVER_VPN_IP       "100.64.0.1" 
#define MQTT_BROKER_URL     "mqtt://100.64.0.1:1883"

// IP address of the TP-Link smart plug on the local VLAN 20
#define TARGET_PLUG_IP      "192.168.20.10"
#define TARGET_PLUG_ID      1
// ─────────────────────────────────────────────────────────────────────────────

static const char *TAG = "amphive_gateway";

static EventGroupHandle_t wifi_event_group;
#define WIFI_CONNECTED_BIT BIT0

static esp_mqtt_client_handle_t mqtt_client = NULL;
static bool mqtt_connected = false;

// Active session safety watchdog state
static struct {
    bool active;
    uint32_t start_time_s;
    uint32_t max_duration_s;
    float start_energy_kwh;
    float max_kwh;
} active_session = {0};

// --- Forward Declarations ---
static void start_mqtt_client(void);
static void telemetry_task(void *pvParameters);

// ─── WiFi Event Handler ───────────────────────────────────────────────────────
static void wifi_event_handler(void *arg, esp_event_base_t event_base,
                                int32_t event_id, void *event_data) {
    if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (event_base == WIFI_EVENT && event_id == WIFI_EVENT_STA_DISCONNECTED) {
        ESP_LOGW(TAG, "WiFi connection dropped. Retrying...");
        esp_wifi_connect();
    } else if (event_base == IP_EVENT && event_id == IP_EVENT_STA_GOT_IP) {
        ip_event_got_ip_t *event = (ip_event_got_ip_t *)event_data;
        ESP_LOGI(TAG, "WiFi connected. Local IP: " IPSTR, IP2STR(&event->ip_info.ip));
        xEventGroupSetBits(wifi_event_group, WIFI_CONNECTED_BIT);
    }
}

// ─── WiFi Initialization ──────────────────────────────────────────────────────
static void wifi_init(void) {
    wifi_event_group = xEventGroupCreate();

    ESP_ERROR_CHECK(nvs_flash_init());
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    esp_netif_create_default_wifi_sta();

    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&cfg));

    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID,
                                                &wifi_event_handler, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, IP_EVENT_STA_GOT_IP,
                                                &wifi_event_handler, NULL));

    wifi_config_t wifi_cfg = {
        .sta = {
            .ssid     = WIFI_SSID,
            .password = WIFI_PASSWORD,
        },
    };

    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &wifi_cfg));
    ESP_ERROR_CHECK(esp_wifi_start());

    ESP_LOGI(TAG, "Connecting to local VLAN WiFi...");
    xEventGroupWaitBits(wifi_event_group, WIFI_CONNECTED_BIT,
                        pdFALSE, pdTRUE, portMAX_DELAY);
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
            char status_topic[64];
            snprintf(status_topic, sizeof(status_topic), "amphive/gateways/%s/status", GATEWAY_ID);
            esp_mqtt_client_publish(mqtt_client, status_topic, "{\"status\":\"online\"}", 0, 1, 1);
            
            // Subscribe to incoming commands for this gateway's plugs
            char command_topic[64];
            snprintf(command_topic, sizeof(command_topic), "amphive/gateways/%s/plugs/+/commands", GATEWAY_ID);
            esp_mqtt_client_subscribe(mqtt_client, command_topic, 1);
            ESP_LOGI(TAG, "Subscribed to commands: %s", command_topic);
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
            // Expected Command payload: {"action": "ON"/"OFF", "max_duration_seconds": X, "max_kwh": Y}
            if (strstr(data, "\"action\":\"ON\"") || strstr(data, "\"action\": \"ON\"")) {
                ESP_LOGI(TAG, "Command: Turning Smart Plug ON.");
                
                // Parse safety configurations (defaults if not present)
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

                // Call local Tapo Driver
                if (tapo_set_power_state(TARGET_PLUG_IP, true) == ESP_OK) {
                    // Update active session watchdog limits
                    active_session.active = true;
                    active_session.start_time_s = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000;
                    active_session.max_duration_s = duration;
                    active_session.max_kwh = kwh_limit;
                    
                    tapo_telemetry_t telemetry;
                    if (tapo_get_telemetry(TARGET_PLUG_IP, &telemetry) == ESP_OK) {
                        active_session.start_energy_kwh = telemetry.energy_kwh;
                    } else {
                        active_session.start_energy_kwh = 0.0f;
                    }
                    
                    ESP_LOGI(TAG, "Session initialized. Limit: %lu s, %f kWh", duration, kwh_limit);
                }
            } else if (strstr(data, "\"action\":\"OFF\"") || strstr(data, "\"action\": \"OFF\"")) {
                ESP_LOGI(TAG, "Command: Turning Smart Plug OFF.");
                tapo_set_power_state(TARGET_PLUG_IP, false);
                active_session.active = false;
            }
            break;
        }
        default:
            break;
    }
}

// ─── MQTT Client Startup ─────────────────────────────────────────────────────
static void start_mqtt_client(void) {
    char lwt_topic[64];
    snprintf(lwt_topic, sizeof(lwt_topic), "amphive/gateways/%s/status", GATEWAY_ID);

    esp_mqtt_client_config_t mqtt_cfg = {
        .broker.address.uri = MQTT_BROKER_URL,
        // Last Will and Testament configuration: publishes "offline" status if gateway drops connection
        .session.last_will = {
            .topic = lwt_topic,
            .message = "{\"status\":\"offline\"}",
            .qos = 1,
            .retain = true,
        }
    };

    mqtt_client = esp_mqtt_client_init(&mqtt_cfg);
    esp_mqtt_client_register_event(mqtt_client, ESP_EVENT_ANY_ID, mqtt_event_handler, NULL);
    esp_mqtt_client_start(mqtt_client);
}

// ─── Telemetry Polling & Watchdog Safety Loop ────────────────────────────────
static void telemetry_task(void *pvParameters) {
    tapo_telemetry_t telemetry;
    char telemetry_topic[64];
    snprintf(telemetry_topic, sizeof(telemetry_topic), "amphive/gateways/%s/telemetry", GATEWAY_ID);

    while (1) {
        if (mqtt_connected) {
            esp_err_t ret = tapo_get_telemetry(TARGET_PLUG_IP, &telemetry);
            if (ret == ESP_OK) {
                // Publish energy metrics to MQTT
                char payload[256];
                snprintf(payload, sizeof(payload),
                         "{\"plug_id\":%d,\"watts\":%.1f,\"kwh\":%.4f,\"voltage\":%.1f,\"current\":%.2f,\"status\":\"%s\"}",
                         TARGET_PLUG_ID,
                         telemetry.power_w,
                         telemetry.energy_kwh,
                         telemetry.voltage_v,
                         telemetry.current_a,
                         active_session.active ? "occupied" : "available");
                
                esp_mqtt_client_publish(mqtt_client, telemetry_topic, payload, 0, 0, 0);
                ESP_LOGI(TAG, "Published telemetry: %s W, %s", 
                         active_session.active ? "Active" : "Idle", payload);

                // Check Session Safety Limits (Watchdog)
                if (active_session.active) {
                    uint32_t current_time_s = xTaskGetTickCount() * portTICK_PERIOD_MS / 1000;
                    uint32_t elapsed_s = current_time_s - active_session.start_time_s;
                    float consumed_kwh = telemetry.energy_kwh - active_session.start_energy_kwh;

                    // Safety 1: Max Duration Exceeded
                    if (elapsed_s >= active_session.max_duration_s) {
                        ESP_LOGE(TAG, "WATCHDOG: Maximum session duration (%lu s) exceeded. Shutting down plug locally!", active_session.max_duration_s);
                        tapo_set_power_state(TARGET_PLUG_IP, false);
                        active_session.active = false;
                    }
                    // Safety 2: Max energy exceeded
                    else if (consumed_kwh >= active_session.max_kwh) {
                        ESP_LOGE(TAG, "WATCHDOG: Session energy consumption limit (%f kWh) reached. Shutting down plug locally!", active_session.max_kwh);
                        tapo_set_power_state(TARGET_PLUG_IP, false);
                        active_session.active = false;
                    }
                    // Safety 3: Thermal Cut-off (Plug reports high temperature)
                    else if (telemetry.temperature_c > 75.0f) {
                        ESP_LOGE(TAG, "THERMAL ALARM: Plug temperature at %.1f C (threshold 75 C). Shutting down plug locally!", telemetry.temperature_c);
                        tapo_set_power_state(TARGET_PLUG_IP, false);
                        active_session.active = false;
                        
                        // Publish alarm status
                        char alarm_topic[128];
                        snprintf(alarm_topic, sizeof(alarm_topic), "amphive/gateways/%s/alarms", GATEWAY_ID);
                        esp_mqtt_client_publish(mqtt_client, alarm_topic, "{\"error\":\"THERMAL_CUTOFF\"}", 0, 1, 0);
                    }
                }
            } else {
                ESP_LOGE(TAG, "Failed to connect to Tapo plug locally.");
            }
        }
        vTaskDelay(pdMS_TO_TICKS(15000)); // Poll every 15 seconds
    }
}

// ─── MicroLink VPN Tunnel Task ───────────────────────────────────────────────
static void microlink_task(void *pvParameters) {
    ESP_LOGI(TAG, "=== Starting AmpHive MicroLink Network Client ===");

    microlink_config_t config;
    microlink_get_default_config(&config);
    config.auth_key     = TS_AUTH_KEY;
    config.device_name  = DEVICE_NAME;
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

                // Now that the secure VPN tunnel is active, boot the MQTT Client over the tunnel IP
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
    // 1. Initialize local networks and WiFi STA mode
    wifi_init();

    // 2. Initialize local smart plug drivers
    tapo_init();

    // 3. Start telemetry and safety watchdog loops
    xTaskCreate(telemetry_task, "telemetry_safety", 4096, NULL, 5, NULL);

    // 4. Start Tailscale connection client (runs with large 32KB stack in external PSRAM)
    xTaskCreate(microlink_task, "microlink_vpn", 32768, NULL, 6, NULL);
}
