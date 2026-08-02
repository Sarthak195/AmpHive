/*
 * Tapo P110 driver — real KLAP v2 implementation.
 *
 * KLAP is TP-Link's local encryption protocol (SHA1/SHA256/AES-128-CBC, no RSA).
 * Verified against a real P110 (fw 1.1.3) via tools/klap_probe.py. Flow:
 *
 *   auth_hash = SHA256( SHA1(email) || SHA1(password) )
 *   handshake1: POST /app/handshake1   body = local_seed(16 random)
 *               resp(48) = remote_seed(16) || server_hash(32)
 *               verify SHA256(local_seed||remote_seed||auth_hash) == server_hash
 *               capture Set-Cookie: TP_SESSIONID=...
 *   handshake2: POST /app/handshake2   body = SHA256(remote_seed||local_seed||auth_hash)
 *   session keys (S = local_seed||remote_seed||auth_hash):
 *               key = SHA256("lsk"||S)[0:16]
 *               ivf = SHA256("iv"||S); iv = ivf[0:12]; seq = int32_be_signed(ivf[28:32])
 *               sig = SHA256("ldk"||S)[0:28]
 *   request:    seq++; full_iv = iv || be32(seq)
 *               ct = AES-128-CBC(PKCS7(plaintext), key, full_iv)
 *               body = SHA256(sig||be32(seq)||ct) || ct   -> POST /app/request?seq=N
 *               resp = signature(32) || ct ; decrypt with key + full_iv(same seq)
 *
 * Multi-plug (TD#20): the account (auth_hash) and nominal voltage are shared and
 * stay module-global; the KLAP session and the energy integrator are per-plug,
 * held in a `tapo_plug_t` allocated by tapo_plug_create(). A per-plug mutex
 * serialises that plug's set/get so its energy read-modify-write and KLAP seq
 * counter can't be raced by the telemetry task vs. the command handler.
 */
#include "tapo_protocol.h"
#include <string.h>
#include <strings.h>
#include <stdlib.h>
#include <stdio.h>
#include <math.h>
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_http_client.h"
#include "nvs.h"
#include "session_nvs.h"   /* PLUG_IP_MAX_LEN — shared with main.c's plug-slot IP buffer so a
                             * roster "host:port" entry (bare-IP-or-":port", see PLUG_IP_MAX_LEN's
                             * own comment) fits identically in every buffer that stores it. */
#include "mbedtls/md.h"
#if MBEDTLS_MAJOR_VERSION >= 4
#define MBEDTLS_DECLARE_PRIVATE_IDENTIFIERS
#include "mbedtls/private/aes.h"
#else
#include "mbedtls/aes.h"
#endif

static const char *TAG = "tapo_klap";

#define TAPO_NOMINAL_TEMP_C   25.0f   /* placeholder; P110 has no temp sensor */
#define KLAP_SESSION_TTL_MS   (60 * 60 * 1000)  /* re-handshake hourly (plug TIMEOUT is 24h) */
#define KLAP_RESP_CAP         2048

/* ── shared account state (one Tapo account owns every plug) ──────────────── */
static bool             s_initialized = false;
static char             s_email[64];
static char             s_password[64];
static float            s_nominal_voltage = 230.0f;
static uint8_t          s_auth_hash[32];

#define ENERGY_NVS_NAMESPACE         "energy"
/* Persist at most once per this many Wh accrued. Writes therefore only happen
 * while actually charging (idle power ~0 never crosses the threshold), which
 * bounds NVS wear while capping the worst-case crash energy loss to <threshold. */
#define ENERGY_PERSIST_THRESHOLD_WH  50.0

/* ── offline/unmetered-consumption reconciliation (idle-baseline) ─────────── */
#define BASELINE_UNSET                 -1.0f   /* sentinel: no baseline yet (fresh plug / NVS wipe) */
#define UNMETERED_THRESHOLD_KWH        0.01f   /* 10 Wh -- ignore standby-draw / rounding noise */
#define BASELINE_RESET_EPSILON_KWH     0.001f  /* 1 Wh -- rounding jitter on an essentially-flat reading */
#define BASELINE_PERSIST_THRESHOLD_KWH 0.01f   /* throttle NVS writes (mirrors ENERGY_PERSIST_THRESHOLD_WH) */

typedef struct {
    bool       valid;
    uint8_t    key[16];
    uint8_t    iv[12];
    uint8_t    sig[28];
    int32_t    seq;
    char       cookie[80];   /* "TP_SESSIONID=...." */
    TickType_t expiry;
} klap_session_t;

/* ── per-plug driver context ──────────────────────────────────────────────── */
struct tapo_plug_s {
    char             ip[PLUG_IP_MAX_LEN];   /* bare IP, or "IP:port" for the emulator bench */
    int              plug_id;
    SemaphoreHandle_t mutex;
    klap_session_t   sess;
    /* driver-side monotonic energy meter (the session watchdog needs a monotonic
     * kWh). Persisted to NVS (key "wh_<plug_id>") so a mid-session reboot doesn't
     * reset the meter to 0 while the session's start_energy_kwh is restored to its
     * pre-reboot value — that made consumed_kwh (= meter - start) go negative and
     * disarmed the energy watchdog until the meter re-accrued. Restoring the meter
     * keeps both on the same scale, so the watchdog stays armed across a crash. */
    double     energy_wh;
    TickType_t energy_last_tick;
    double     energy_persisted_wh;
    float      energy_last_power_w;   /* last power sample, for trapezoidal integration */
    char       nvs_key[16];           /* "wh_<plug_id>" */
    /* Offline/unmetered-consumption idle baseline (see
     * tapo_plug_reconcile_idle_baseline()): the plug's own today_energy/
     * month_energy readings the last time we were confident nothing unbilled
     * was happening (no active AmpHive session). BASELINE_UNSET (<0) until
     * the first idle reading ever seeds it. Persisted to NVS (key
     * "bl_<plug_id>", same namespace as the energy meter above) so it
     * survives an ESP32 reboot -- baseline_persisted_month_kwh tracks what's
     * actually flashed, throttling NVS writes the same way energy_wh does. */
    float      baseline_today_kwh;
    float      baseline_month_kwh;
    float      baseline_persisted_month_kwh;
    char       baseline_nvs_key[16];  /* "bl_<plug_id>" */
};

/* Restore the integrator from NVS (blob = the exact double, so no overflow or
 * precision loss). No-op when nothing is stored (fresh plug). */
static void energy_load_nvs(tapo_plug_t *p) {
    nvs_handle_t h;
    if (nvs_open(ENERGY_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
    double wh = 0.0;
    size_t sz = sizeof(wh);
    if (nvs_get_blob(h, p->nvs_key, &wh, &sz) == ESP_OK && sz == sizeof(wh)) {
        p->energy_wh = wh;
        p->energy_persisted_wh = wh;
        ESP_LOGI(TAG, "plug %d: restored energy integrator from NVS: %.1f Wh", p->plug_id, wh);
    }
    nvs_close(h);
}

static void energy_persist_nvs(tapo_plug_t *p) {
    nvs_handle_t h;
    if (nvs_open(ENERGY_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    if (nvs_set_blob(h, p->nvs_key, &p->energy_wh, sizeof(p->energy_wh)) == ESP_OK &&
        nvs_commit(h) == ESP_OK) {
        p->energy_persisted_wh = p->energy_wh;
    }
    nvs_close(h);
}

/* Blob shape persisted under baseline_nvs_key -- kept as a tiny local struct
 * (not tapo_energy_reconcile_t) so growing the public result type later
 * can't silently change the NVS wire layout. */
typedef struct { float today_kwh; float month_kwh; } baseline_blob_t;

/* Restore the idle-consumption baseline from NVS. No-op (leaves BASELINE_UNSET)
 * when nothing is stored yet -- a fresh plug or a wiped NVS never reports on
 * its very first idle reading (nothing to compare against). */
static void baseline_load_nvs(tapo_plug_t *p) {
    p->baseline_today_kwh = BASELINE_UNSET;
    p->baseline_month_kwh = BASELINE_UNSET;
    p->baseline_persisted_month_kwh = BASELINE_UNSET;
    nvs_handle_t h;
    if (nvs_open(ENERGY_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return;
    baseline_blob_t blob;
    size_t sz = sizeof(blob);
    if (nvs_get_blob(h, p->baseline_nvs_key, &blob, &sz) == ESP_OK && sz == sizeof(blob)) {
        p->baseline_today_kwh = blob.today_kwh;
        p->baseline_month_kwh = blob.month_kwh;
        p->baseline_persisted_month_kwh = blob.month_kwh;
        ESP_LOGI(TAG, "plug %d: restored idle energy baseline from NVS (today=%.3f kWh, month=%.3f kWh)",
                 p->plug_id, blob.today_kwh, blob.month_kwh);
    }
    nvs_close(h);
}

/* Unconditional flush (caller decides whether the drift crosses
 * BASELINE_PERSIST_THRESHOLD_KWH -- same split as energy_persist_nvs, whose
 * caller (tapo_plug_get_telemetry) does its own threshold check). */
static void baseline_persist_nvs(tapo_plug_t *p) {
    nvs_handle_t h;
    if (nvs_open(ENERGY_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return;
    baseline_blob_t blob = { p->baseline_today_kwh, p->baseline_month_kwh };
    if (nvs_set_blob(h, p->baseline_nvs_key, &blob, sizeof(blob)) == ESP_OK &&
        nvs_commit(h) == ESP_OK) {
        p->baseline_persisted_month_kwh = p->baseline_month_kwh;
    }
    nvs_close(h);
}

/* ── crypto helpers (stateless) ───────────────────────────────────────────── */
typedef struct { const uint8_t *p; size_t n; } seg_t;

static void sha256_segs(const seg_t *segs, int count, uint8_t out[32]) {
    const mbedtls_md_info_t *info = mbedtls_md_info_from_type(MBEDTLS_MD_SHA256);
    mbedtls_md_context_t c;
    mbedtls_md_init(&c);
    if (!info || mbedtls_md_setup(&c, info, 0) != 0 || mbedtls_md_starts(&c) != 0) {
        mbedtls_md_free(&c);
        memset(out, 0, 32);
        return;
    }
    for (int i = 0; i < count; i++) {
        if (segs[i].p && segs[i].n) mbedtls_md_update(&c, segs[i].p, segs[i].n);
    }
    mbedtls_md_finish(&c, out);
    mbedtls_md_free(&c);
}

static int aes_cbc_crypt(int mode, const uint8_t key[16], const uint8_t iv[16],
                         const uint8_t *in, uint8_t *out, size_t len) {
    mbedtls_aes_context a;
    mbedtls_aes_init(&a);
    uint8_t iv_copy[16];
    memcpy(iv_copy, iv, 16);   /* mbedtls mutates the IV in place */
    int r = (mode == MBEDTLS_AES_ENCRYPT) ? mbedtls_aes_setkey_enc(&a, key, 128)
                                          : mbedtls_aes_setkey_dec(&a, key, 128);
    if (r == 0) r = mbedtls_aes_crypt_cbc(&a, mode, len, iv_copy, in, out);
    mbedtls_aes_free(&a);
    return r;
}

static void be32(int32_t v, uint8_t out[4]) {
    out[0] = (uint8_t)(v >> 24); out[1] = (uint8_t)(v >> 16);
    out[2] = (uint8_t)(v >> 8);  out[3] = (uint8_t)(v);
}

/* ── HTTP layer (esp_http_client, stateless) ──────────────────────────────── */
typedef struct {
    uint8_t *body;
    int      body_len;
    int      body_cap;
    char    *setcookie;
    int      setcookie_cap;
} http_ctx_t;

static esp_err_t http_event_cb(esp_http_client_event_t *evt) {
    http_ctx_t *ctx = (http_ctx_t *)evt->user_data;
    if (!ctx) return ESP_OK;
    switch (evt->event_id) {
        case HTTP_EVENT_ON_HEADER:
            if (ctx->setcookie && evt->header_key &&
                strcasecmp(evt->header_key, "Set-Cookie") == 0) {
                strncpy(ctx->setcookie, evt->header_value, ctx->setcookie_cap - 1);
                ctx->setcookie[ctx->setcookie_cap - 1] = '\0';
            }
            break;
        case HTTP_EVENT_ON_DATA:
            if (ctx->body && ctx->body_len < ctx->body_cap) {
                int n = evt->data_len;
                if (ctx->body_len + n > ctx->body_cap) n = ctx->body_cap - ctx->body_len;
                if (n > 0) { memcpy(ctx->body + ctx->body_len, evt->data, n); ctx->body_len += n; }
            }
            break;
        default: break;
    }
    return ESP_OK;
}

/* POST binary body; returns HTTP status in *status and body bytes in resp. */
static esp_err_t klap_http_post(const char *url, const char *cookie,
                                const uint8_t *body, size_t body_len,
                                uint8_t *resp, int resp_cap, int *resp_len,
                                char *setcookie_out, int setcookie_cap, int *status) {
    http_ctx_t ctx = { .body = resp, .body_len = 0, .body_cap = resp_cap,
                       .setcookie = setcookie_out, .setcookie_cap = setcookie_cap };
    esp_http_client_config_t cfg = {
        .url = url,
        .method = HTTP_METHOD_POST,
        .event_handler = http_event_cb,
        .user_data = &ctx,
        .timeout_ms = 4000,
        .disable_auto_redirect = true,
    };
    esp_http_client_handle_t c = esp_http_client_init(&cfg);
    if (!c) return ESP_FAIL;
    esp_http_client_set_header(c, "Content-Type", "application/octet-stream");
    if (cookie && cookie[0]) esp_http_client_set_header(c, "Cookie", cookie);
    esp_http_client_set_post_field(c, (const char *)body, body_len);
    esp_err_t err = esp_http_client_perform(c);
    if (err == ESP_OK) {
        if (status)   *status = esp_http_client_get_status_code(c);
        if (resp_len) *resp_len = ctx.body_len;
    }
    esp_http_client_cleanup(c);
    return err;
}

static void extract_session_cookie(const char *setcookie, char *out, int out_cap) {
    out[0] = '\0';
    if (!setcookie) return;
    const char *p = strstr(setcookie, "TP_SESSIONID=");
    if (!p) return;
    int i = 0;
    while (p[i] && p[i] != ';' && i < out_cap - 1) { out[i] = p[i]; i++; }
    out[i] = '\0';
}

/* ── KLAP handshake (operates on a per-plug session) ──────────────────────── */
static esp_err_t klap_handshake(const char *ip, klap_session_t *sess) {
    uint8_t local_seed[16];
    esp_fill_random(local_seed, sizeof(local_seed));

    char url[80];
    snprintf(url, sizeof(url), "http://%s/app/handshake1", ip);

    uint8_t resp[64];
    char setcookie[160] = {0};
    int resp_len = 0, status = 0;
    esp_err_t err = klap_http_post(url, NULL, local_seed, 16, resp, sizeof(resp),
                                   &resp_len, setcookie, sizeof(setcookie), &status);
    if (err != ESP_OK) { ESP_LOGE(TAG, "handshake1 transport: %s", esp_err_to_name(err)); return err; }
    if (status != 200 || resp_len != 48) {
        ESP_LOGE(TAG, "handshake1 unexpected resp status=%d len=%d (KLAP expected 200/48)", status, resp_len);
        return ESP_FAIL;
    }

    uint8_t remote_seed[16], server_hash[32];
    memcpy(remote_seed, resp, 16);
    memcpy(server_hash, resp + 16, 32);

    uint8_t expected[32];
    seg_t v[3] = { {local_seed, 16}, {remote_seed, 16}, {s_auth_hash, 32} };
    sha256_segs(v, 3, expected);
    if (memcmp(expected, server_hash, 32) != 0) {
        ESP_LOGE(TAG, "handshake1 auth mismatch — check Tapo email/password + Third-Party Compatibility");
        return ESP_FAIL;
    }

    char cookie[80];
    extract_session_cookie(setcookie, cookie, sizeof(cookie));
    if (!cookie[0]) { ESP_LOGE(TAG, "handshake1: no TP_SESSIONID cookie"); return ESP_FAIL; }

    uint8_t hs2[32];
    seg_t h2[3] = { {remote_seed, 16}, {local_seed, 16}, {s_auth_hash, 32} };
    sha256_segs(h2, 3, hs2);

    snprintf(url, sizeof(url), "http://%s/app/handshake2", ip);
    uint8_t r2[16];
    int r2_len = 0;
    status = 0;
    err = klap_http_post(url, cookie, hs2, 32, r2, sizeof(r2), &r2_len, NULL, 0, &status);
    if (err != ESP_OK || status != 200) {
        ESP_LOGE(TAG, "handshake2 failed: %s status=%d", esp_err_to_name(err), status);
        return ESP_FAIL;
    }

    /* derive session key / iv+seq / sig — all SHA256(prefix || local || remote || auth) */
    seg_t base[4];
    base[1] = (seg_t){ local_seed, 16 };
    base[2] = (seg_t){ remote_seed, 16 };
    base[3] = (seg_t){ s_auth_hash, 32 };
    uint8_t tmp[32];

    base[0] = (seg_t){ (const uint8_t *)"lsk", 3 };
    sha256_segs(base, 4, tmp);
    memcpy(sess->key, tmp, 16);

    base[0] = (seg_t){ (const uint8_t *)"iv", 2 };
    sha256_segs(base, 4, tmp);
    memcpy(sess->iv, tmp, 12);
    uint32_t seq0 = ((uint32_t)tmp[28] << 24) | ((uint32_t)tmp[29] << 16) |
                    ((uint32_t)tmp[30] << 8)  |  (uint32_t)tmp[31];
    sess->seq = (int32_t)seq0;

    base[0] = (seg_t){ (const uint8_t *)"ldk", 3 };
    sha256_segs(base, 4, tmp);
    memcpy(sess->sig, tmp, 28);

    strncpy(sess->cookie, cookie, sizeof(sess->cookie) - 1);
    sess->cookie[sizeof(sess->cookie) - 1] = '\0';
    sess->expiry = xTaskGetTickCount() + pdMS_TO_TICKS(KLAP_SESSION_TTL_MS);
    sess->valid = true;
    ESP_LOGI(TAG, "KLAP session established with %s", ip);
    return ESP_OK;
}

/* Perform one encrypted request. Returns ESP_ERR_INVALID_STATE on HTTP 403
 * (session expired) so the caller can re-handshake and retry. */
static esp_err_t klap_request_once(const char *ip, klap_session_t *sess, const char *inner_json,
                                   uint8_t *out_plain, int out_cap, int *out_len) {
    size_t pt_len = strlen(inner_json);
    size_t padded = ((pt_len / 16) + 1) * 16;          /* PKCS7 always adds 1..16 */
    uint8_t pad = (uint8_t)(padded - pt_len);

    esp_err_t result = ESP_FAIL;
    uint8_t *pt = malloc(padded);
    uint8_t *ct = malloc(padded);
    uint8_t *body = malloc(32 + padded);
    uint8_t *resp = malloc(KLAP_RESP_CAP);
    if (!pt || !ct || !body || !resp) { result = ESP_ERR_NO_MEM; goto done; }

    memcpy(pt, inner_json, pt_len);
    memset(pt + pt_len, pad, pad);

    int32_t seq = ++sess->seq;
    uint8_t full_iv[16];
    memcpy(full_iv, sess->iv, 12);
    be32(seq, full_iv + 12);

    if (aes_cbc_crypt(MBEDTLS_AES_ENCRYPT, sess->key, full_iv, pt, ct, padded) != 0) goto done;

    uint8_t seqb[4]; be32(seq, seqb);
    uint8_t sig[32];
    seg_t ss[3] = { {sess->sig, 28}, {seqb, 4}, {ct, padded} };
    sha256_segs(ss, 3, sig);

    memcpy(body, sig, 32);
    memcpy(body + 32, ct, padded);

    char url[96];
    snprintf(url, sizeof(url), "http://%s/app/request?seq=%ld", ip, (long)seq);

    int resp_len = 0, status = 0;
    esp_err_t err = klap_http_post(url, sess->cookie, body, 32 + padded,
                                   resp, KLAP_RESP_CAP, &resp_len, NULL, 0, &status);
    if (err != ESP_OK) { ESP_LOGE(TAG, "request transport: %s", esp_err_to_name(err)); result = ESP_FAIL; goto done; }
    if (status == 403) { result = ESP_ERR_INVALID_STATE; goto done; }
    if (status != 200 || resp_len <= 32) { ESP_LOGE(TAG, "request bad resp status=%d len=%d", status, resp_len); result = ESP_FAIL; goto done; }

    size_t ct_len = resp_len - 32;
    if (ct_len % 16 != 0 || (int)ct_len >= out_cap) { ESP_LOGE(TAG, "resp ct_len %d invalid", (int)ct_len); result = ESP_FAIL; goto done; }
    if (aes_cbc_crypt(MBEDTLS_AES_DECRYPT, sess->key, full_iv, resp + 32, out_plain, ct_len) != 0) goto done;

    int plen = (int)ct_len;
    uint8_t padv = out_plain[plen - 1];
    if (padv >= 1 && padv <= 16 && padv <= plen) plen -= padv;   /* strip PKCS7 */
    out_plain[plen] = '\0';
    *out_len = plen;
    result = ESP_OK;

done:
    free(pt); free(ct); free(body); free(resp);
    return result;
}

/* Ensure a live session, run the request, re-handshake once on 403. */
static esp_err_t klap_request(const char *ip, klap_session_t *sess, const char *inner_json,
                              uint8_t *out_plain, int out_cap, int *out_len) {
    if (!sess->valid || (int32_t)(xTaskGetTickCount() - sess->expiry) >= 0) {
        if (klap_handshake(ip, sess) != ESP_OK) return ESP_FAIL;
    }
    esp_err_t rc = klap_request_once(ip, sess, inner_json, out_plain, out_cap, out_len);
    if (rc == ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "session rejected (403) — re-handshaking");
        sess->valid = false;
        if (klap_handshake(ip, sess) != ESP_OK) return ESP_FAIL;
        rc = klap_request_once(ip, sess, inner_json, out_plain, out_cap, out_len);
    }
    return rc;
}

/* ── minimal JSON extraction (compact Tapo responses; no cJSON dependency) ──
 * Keys are passed WITH the trailing colon, e.g. "\"current_power\":". */
static int json_int(const char *s, const char *key, long *out) {
    const char *p = strstr(s, key);
    if (!p) return 0;
    return sscanf(p + strlen(key), "%ld", out) == 1;   /* %ld skips whitespace */
}
static bool json_true(const char *s, const char *key) {
    const char *p = strstr(s, key);
    if (!p) return false;
    p += strlen(key);
    while (*p == ' ') p++;
    return strncmp(p, "true", 4) == 0;
}
/* A P110 safety status ("overheat_status"/"overcurrent_status") is "normal" when
 * healthy. Absent field or "normal" -> not abnormal (don't false-trip a cutoff). */
static bool json_status_abnormal(const char *s, const char *key) {
    const char *p = strstr(s, key);
    if (!p) return false;
    p += strlen(key);
    while (*p == ' ') p++;
    return strncmp(p, "\"normal\"", 8) != 0;
}

/* Parse "error_code" from a decrypted response; returns the code (or -1). */
static int response_error_code(const char *plain) {
    long ec = -1;
    json_int(plain, "\"error_code\":", &ec);
    return (int)ec;
}

/* ── public API ───────────────────────────────────────────────────────────── */
esp_err_t tapo_init(const char *tapo_email, const char *tapo_password, float nominal_voltage) {
    if (!tapo_email || !tapo_password || !tapo_email[0]) {
        ESP_LOGE(TAG, "tapo_init: missing credentials");
        return ESP_ERR_INVALID_ARG;
    }
    strncpy(s_email, tapo_email, sizeof(s_email) - 1);
    s_email[sizeof(s_email) - 1] = '\0';
    strncpy(s_password, tapo_password, sizeof(s_password) - 1);
    s_password[sizeof(s_password) - 1] = '\0';
    s_nominal_voltage = (nominal_voltage > 1.0f) ? nominal_voltage : 230.0f;

    /* auth_hash = SHA256( SHA1(email) || SHA1(password) ) */
    uint8_t su[20], sp[20];
    const mbedtls_md_info_t *sha1 = mbedtls_md_info_from_type(MBEDTLS_MD_SHA1);
    if (!sha1 ||
        mbedtls_md(sha1, (const uint8_t *)s_email, strlen(s_email), su) != 0 ||
        mbedtls_md(sha1, (const uint8_t *)s_password, strlen(s_password), sp) != 0) {
        ESP_LOGE(TAG, "Failed to derive KLAP auth hash (SHA1 unavailable)");
        return ESP_FAIL;
    }
    seg_t ah[2] = { {su, 20}, {sp, 20} };
    sha256_segs(ah, 2, s_auth_hash);

    s_initialized = true;
    ESP_LOGI(TAG, "Tapo KLAP driver initialized (account %s, nominal %.0f V)", s_email, s_nominal_voltage);
    return ESP_OK;
}

tapo_plug_t *tapo_plug_create(int plug_id, const char *local_ip) {
    if (!s_initialized) { ESP_LOGE(TAG, "tapo_plug_create before tapo_init"); return NULL; }
    if (!local_ip || !local_ip[0]) { ESP_LOGE(TAG, "tapo_plug_create: missing ip"); return NULL; }

    tapo_plug_t *p = calloc(1, sizeof(*p));
    if (!p) { ESP_LOGE(TAG, "tapo_plug_create: OOM"); return NULL; }
    p->mutex = xSemaphoreCreateMutex();
    if (!p->mutex) { free(p); ESP_LOGE(TAG, "tapo_plug_create: mutex OOM"); return NULL; }

    p->plug_id = plug_id;
    strncpy(p->ip, local_ip, sizeof(p->ip) - 1);
    p->ip[sizeof(p->ip) - 1] = '\0';
    /* NVS key max is 15 chars; "wh_"/"bl_" + plug_id digits fits realistic ids. */
    snprintf(p->nvs_key, sizeof(p->nvs_key), "wh_%d", plug_id);
    snprintf(p->baseline_nvs_key, sizeof(p->baseline_nvs_key), "bl_%d", plug_id);

    energy_load_nvs(p);    /* resume the cross-reboot integrator so the watchdog stays armed */
    baseline_load_nvs(p);  /* resume the offline/unmetered-consumption idle baseline */
    ESP_LOGI(TAG, "Tapo plug context created (id=%d, ip=%s)", plug_id, p->ip);
    return p;
}

void tapo_plug_destroy(tapo_plug_t *plug) {
    if (!plug) return;
    /* Flush the final meter so a plug re-added later resumes its total
       (energy_load_nvs keys on "wh_<plug_id>") rather than restarting from 0. */
    energy_persist_nvs(plug);
    baseline_persist_nvs(plug);
    ESP_LOGI(TAG, "Tapo plug context destroyed (id=%d, ip=%s)", plug->plug_id, plug->ip);
    if (plug->mutex) vSemaphoreDelete(plug->mutex);
    free(plug);
}

void tapo_plug_set_ip(tapo_plug_t *plug, const char *local_ip) {
    if (!plug || !local_ip || !local_ip[0]) return;
    xSemaphoreTake(plug->mutex, portMAX_DELAY);
    if (strncmp(plug->ip, local_ip, sizeof(plug->ip)) != 0) {
        strncpy(plug->ip, local_ip, sizeof(plug->ip) - 1);
        plug->ip[sizeof(plug->ip) - 1] = '\0';
        plug->sess.valid = false;   /* IP changed — force a fresh handshake */
        ESP_LOGI(TAG, "plug %d: IP updated to %s (session invalidated)", plug->plug_id, plug->ip);
    }
    xSemaphoreGive(plug->mutex);
}

void tapo_plug_reassign_id(tapo_plug_t *plug, int new_plug_id) {
    if (!plug || plug->plug_id == new_plug_id) return;
    xSemaphoreTake(plug->mutex, portMAX_DELAY);
    plug->plug_id = new_plug_id;
    snprintf(plug->nvs_key, sizeof(plug->nvs_key), "wh_%d", new_plug_id);
    snprintf(plug->baseline_nvs_key, sizeof(plug->baseline_nvs_key), "bl_%d", new_plug_id);
    /* Re-seat the energy integrator AND the idle-consumption baseline on the
       new plug's NVS keys. Only ever called on an idle provisional slot, so
       resetting the scale is safe — the next session captures a fresh
       baseline at ON, and the next idle poll reseeds the idle baseline. */
    plug->energy_wh = 0.0;
    plug->energy_persisted_wh = 0.0;
    plug->energy_last_tick = 0;
    plug->energy_last_power_w = 0.0f;
    energy_load_nvs(plug);
    baseline_load_nvs(plug);
    xSemaphoreGive(plug->mutex);
    ESP_LOGI(TAG, "plug context reassigned to id %d (energy key %s)", new_plug_id, plug->nvs_key);
}

esp_err_t tapo_plug_set_power(tapo_plug_t *plug, bool turn_on) {
    if (!plug) return ESP_ERR_INVALID_ARG;
    if (!s_initialized) { ESP_LOGE(TAG, "tapo_init not called"); return ESP_FAIL; }

    char json[96];
    snprintf(json, sizeof(json),
             "{\"method\":\"set_device_info\",\"params\":{\"device_on\":%s}}",
             turn_on ? "true" : "false");

    uint8_t plain[256];
    int plen = 0;
    xSemaphoreTake(plug->mutex, portMAX_DELAY);
    esp_err_t rc = klap_request(plug->ip, &plug->sess, json, plain, sizeof(plain), &plen);
    xSemaphoreGive(plug->mutex);
    if (rc != ESP_OK) { ESP_LOGE(TAG, "plug %d: set_power %s failed", plug->plug_id, turn_on ? "ON" : "OFF"); return rc; }

    int ec = response_error_code((const char *)plain);
    if (ec != 0) { ESP_LOGE(TAG, "set_device_info error_code=%d", ec); return ESP_FAIL; }
    ESP_LOGI(TAG, "Plug %s (id=%d) turned %s", plug->ip, plug->plug_id, turn_on ? "ON" : "OFF");
    return ESP_OK;
}

esp_err_t tapo_plug_get_telemetry(tapo_plug_t *plug, tapo_telemetry_t *out) {
    if (!plug || !out) return ESP_ERR_INVALID_ARG;
    if (!s_initialized) { ESP_LOGE(TAG, "tapo_init not called"); return ESP_FAIL; }

    memset(out, 0, sizeof(*out));
    out->voltage_v = s_nominal_voltage;
    out->temperature_c = TAPO_NOMINAL_TEMP_C;

    uint8_t *plain = malloc(KLAP_RESP_CAP);
    if (!plain) return ESP_ERR_NO_MEM;
    int plen = 0;
    float power_w = 0.0f;
    /* Real per-phase measurements from the P110 energy monitor. current_a is
     * REAL (measured amps) when the plug reports current_ma; voltage_v is REAL
     * when it reports voltage_mv. A P110 that omits either field falls back to
     * the derived current / configured nominal voltage below (older firmware). */
    float meas_current_a = -1.0f;   /* <0 → not reported, derive from power */
    float meas_voltage_v = -1.0f;   /* <0 → not reported, use nominal */
    /* Plug-side calendar counters (also from get_energy_usage -- no extra KLAP
     * round trip). Feed tapo_plug_reconcile_idle_baseline() below; -1 sentinel
     * when the field is absent (see the tapo_telemetry_t doc comment). */
    float meas_today_kwh = -1.0f;
    float meas_month_kwh = -1.0f;

    xSemaphoreTake(plug->mutex, portMAX_DELAY);

    /* 1) power (+ energy source, + real current/voltage) */
    esp_err_t rc = klap_request(plug->ip, &plug->sess, "{\"method\":\"get_energy_usage\"}", plain, KLAP_RESP_CAP, &plen);
    if (rc != ESP_OK) { xSemaphoreGive(plug->mutex); free(plain); return rc; }
    {
        long mw = 0;   /* get_energy_usage.current_power is in milliwatts */
        if (json_int((const char *)plain, "\"current_power\":", &mw)) power_w = (float)mw / 1000.0f;
        long cur_ma = 0;   /* current_ma: real RMS current in milliamps */
        if (json_int((const char *)plain, "\"current_ma\":", &cur_ma)) meas_current_a = (float)cur_ma / 1000.0f;
        long volt_mv = 0;  /* voltage_mv: real RMS voltage in millivolts */
        if (json_int((const char *)plain, "\"voltage_mv\":", &volt_mv)) meas_voltage_v = (float)volt_mv / 1000.0f;
        long today_wh = 0, month_wh = 0;   /* today_energy/month_energy are plain Wh integers */
        if (json_int((const char *)plain, "\"today_energy\":", &today_wh)) meas_today_kwh = (float)today_wh / 1000.0f;
        if (json_int((const char *)plain, "\"month_energy\":", &month_wh)) meas_month_kwh = (float)month_wh / 1000.0f;
    }

    /* 2) state + safety statuses */
    rc = klap_request(plug->ip, &plug->sess, "{\"method\":\"get_device_info\"}", plain, KLAP_RESP_CAP, &plen);
    if (rc == ESP_OK) {
        out->device_on   = json_true((const char *)plain, "\"device_on\":");
        out->overheated  = json_status_abnormal((const char *)plain, "\"overheat_status\":");
        out->overcurrent = json_status_abnormal((const char *)plain, "\"overcurrent_status\":");
    }

    /* monotonic energy integrator (Wh) — robust vs the plug's daily today_energy
     * reset. Updated INSIDE the per-plug mutex: the telemetry task and the
     * ON-handler's baseline read both call tapo_plug_get_telemetry for the same
     * plug and can overlap, so an unlocked read-modify-write of
     * energy_last_tick / energy_wh could double-count or drop an integration slice. */
    TickType_t now = xTaskGetTickCount();
    if (plug->energy_last_tick != 0) {
        float dt_h = (float)(now - plug->energy_last_tick) * portTICK_PERIOD_MS / 3600000.0f;
        /* Trapezoidal rule: average the previous and current power samples over
         * the interval instead of holding the last sample constant (left
         * rectangle). For a load that ramps between polls — the norm for EV
         * charging at the default 10 s cadence — the rectangle rule systematically
         * over/under-counts each ramp; the trapezoid halves that error for the
         * same samples. First sample after boot has no predecessor, so it seeds
         * energy_last_power_w without accruing. */
        plug->energy_wh += ((double)plug->energy_last_power_w + (double)power_w) * 0.5 * dt_h;
    }
    plug->energy_last_tick = now;
    plug->energy_last_power_w = power_w;
    double energy_wh_snapshot = plug->energy_wh;

    /* Persist across reboots (throttled by accrual threshold) so a mid-session
     * crash doesn't disarm the energy watchdog. Kept under the lock so the NVS
     * write and the energy_persisted_wh update stay consistent; it is rare
     * (only every ENERGY_PERSIST_THRESHOLD_WH) and cheap next to the KLAP HTTP
     * calls already made above under the same lock. */
    if (plug->energy_wh - plug->energy_persisted_wh >= ENERGY_PERSIST_THRESHOLD_WH) {
        energy_persist_nvs(plug);
    }

    xSemaphoreGive(plug->mutex);
    free(plain);

    out->power_w   = power_w;
    /* Prefer the plug's REAL measured voltage/current; fall back to nominal /
     * derived only when the P110 firmware doesn't report them. Both are published
     * to 2 decimals by the telemetry task and consumed by the current-cap
     * watchdog, so they must be the measured values whenever available. */
    if (meas_voltage_v >= 0.0f) out->voltage_v = meas_voltage_v;
    if (meas_current_a >= 0.0f) {
        out->current_a = meas_current_a;
    } else {
        out->current_a = (out->voltage_v > 1.0f) ? power_w / out->voltage_v : 0.0f;
    }
    out->energy_kwh = (float)(energy_wh_snapshot / 1000.0);
    out->today_energy_kwh = meas_today_kwh;
    out->month_energy_kwh = meas_month_kwh;

    return ESP_OK;
}

esp_err_t tapo_plug_reconcile_idle_baseline(tapo_plug_t *plug, float today_kwh, float month_kwh,
                                             bool can_report, tapo_energy_reconcile_t *out) {
    if (!plug || !out) return ESP_ERR_INVALID_ARG;
    memset(out, 0, sizeof(*out));
    if (today_kwh < 0.0f || month_kwh < 0.0f) return ESP_OK;  /* plug didn't report them this poll */

    xSemaphoreTake(plug->mutex, portMAX_DELAY);

    if (plug->baseline_today_kwh < 0.0f || plug->baseline_month_kwh < 0.0f) {
        /* First-ever idle reading (fresh plug / NVS wipe) -- nothing to
           compare against yet. Seed the baseline and move on; never report
           on the very first look. */
        plug->baseline_today_kwh = today_kwh;
        plug->baseline_month_kwh = month_kwh;
        baseline_persist_nvs(plug);
        xSemaphoreGive(plug->mutex);
        return ESP_OK;
    }

    float delta_today = today_kwh - plug->baseline_today_kwh;
    float delta_month = month_kwh - plug->baseline_month_kwh;
    bool  reset_today  = delta_today < -BASELINE_RESET_EPSILON_KWH;
    bool  reset_month  = delta_month < -BASELINE_RESET_EPSILON_KWH;

    bool  reset = reset_today && reset_month;   /* both regressed -> full reset, nothing to report */
    float candidate = 0.0f;
    if (!reset_today && !reset_month) {
        /* Prefer whichever climbed more -- normally they move together;
           month_energy is the tighter signal across a multi-day gap since it
           resets far less often than today_energy (nightly). */
        candidate = (delta_month > delta_today) ? delta_month : delta_today;
    } else if (reset_today && !reset_month) {
        /* today rolled over (midnight) mid-gap but month kept climbing --
           month's delta alone is still a legitimate, if slightly
           conservative (may itself have partly rolled), estimate. */
        candidate = delta_month;
    } else if (reset_month && !reset_today) {
        /* month itself just rolled over (new billing month) or reset -- fall
           back to today's delta as a lower-bound estimate. */
        candidate = delta_today;
    }

    bool unmetered = !reset && candidate >= UNMETERED_THRESHOLD_KWH;

    out->unmetered_detected = unmetered;
    out->reset_detected     = reset;
    out->estimated_kwh      = unmetered ? candidate : 0.0f;

    /* Advance (and throttle-flush) the baseline whenever there is nothing
       pending to report, OR the caller is about to actually deliver this
       report (can_report). A detected-but-undelivered episode (MQTT down)
       intentionally leaves the baseline untouched, so the NEXT poll
       recomputes the same (or a larger) delta against the same pre-gap
       reference instead of the report silently evaporating the instant
       connectivity returns -- see the function's header-comment doc. */
    if (reset || !unmetered || can_report) {
        plug->baseline_today_kwh = today_kwh;
        plug->baseline_month_kwh = month_kwh;
        if (fabsf(plug->baseline_month_kwh - plug->baseline_persisted_month_kwh) >= BASELINE_PERSIST_THRESHOLD_KWH) {
            baseline_persist_nvs(plug);
        }
    }

    xSemaphoreGive(plug->mutex);
    return ESP_OK;
}
