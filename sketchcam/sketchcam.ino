/********************  ESP32-CAM PetTracker WS Client  ********************
 * Board: "AI Thinker ESP32-CAM"
 * Librerie richieste: esp32 core, WiFiManager, WebSocketsClient, ArduinoJson, base64, TinyGPSPlus
 * GPS collegato su: TX del GPS --> GPIO12 (ESP32)
 *************************************************************************/

#include <TinyGPS++.h>
#include "esp_camera.h"
#include <WiFi.h>
#include <WebSocketsClient.h>
#include <ArduinoJson.h>
#include <base64.h>
#include <WiFiManager.h>

// =================== CONFIG ===================
static const char* WS_HOST   = "172.20.10.4"; // IP server Python
static const uint16_t WS_PORT = 8765;         // Porta WebSocket server Python

// Frame rate e limiti
static const uint32_t FRAME_INTERVAL_MS_DEFAULT = 150;   // ~6-7 FPS
static const size_t   MAX_JPEG_BYTES             = 120000;
static const size_t   MAX_JSON_CHARS             = 180000;

// Flash LED (AI Thinker: GPIO4)
#define FLASH_LED_PIN 4

// =================== GPS UART ===================
#define GPS_RX 12  // GPIO12 riceve dati dal TX del GPS
#define GPS_TX -1  // Non serve trasmettere al GPS
#define GPS_BAUD 9600

TinyGPSPlus gps;
HardwareSerial GPSSerial(1); // UART1

// =================== PIN CAMERA (AI Thinker) ===================
#define PWDN_GPIO_NUM     32
#define RESET_GPIO_NUM    -1
#define XCLK_GPIO_NUM      0
#define SIOD_GPIO_NUM     26
#define SIOC_GPIO_NUM     27
#define Y9_GPIO_NUM       35
#define Y8_GPIO_NUM       34
#define Y7_GPIO_NUM       39
#define Y6_GPIO_NUM       36
#define Y5_GPIO_NUM       21
#define Y4_GPIO_NUM       19
#define Y3_GPIO_NUM       18
#define Y2_GPIO_NUM        5
#define VSYNC_GPIO_NUM    25
#define HREF_GPIO_NUM     23
#define PCLK_GPIO_NUM     22

// =================== STATI / GLOBALI ===================
WebSocketsClient webSocket;
bool wsConnected = false;

uint32_t frameIntervalMs = FRAME_INTERVAL_MS_DEFAULT;
uint32_t lastFrameMs     = 0;
uint32_t framesSent      = 0;

uint32_t lastHeartbeatMs = 0;
const uint32_t HEARTBEAT_EVERY_MS = 20000;

uint32_t lastSensorReadMs = 0;
const uint32_t SENSOR_READ_EVERY_MS = 2000;

// =================== HELPERS ===================
void sendHello();
void sendHeartbeat();
void sendVideoFrame();
void sendGpsData();
void handleCommand(const String& cmd);
void applyFrameSize(const String& fs);
void setFlipMirror(bool flip, bool mirror);

// =================== WEBSOCKET EVENTS ===================
void webSocketEvent(WStype_t type, uint8_t * payload, size_t length) {
  switch(type) {
    case WStype_DISCONNECTED:
      wsConnected = false;
      Serial.println("[WS] Disconnected");
      break;

    case WStype_CONNECTED: {
      wsConnected = true;
      Serial.printf("[WS] Connected: %s\n", payload);
      sendHello();
      break;
    }

    case WStype_TEXT: {
      DynamicJsonDocument doc(512);
      auto err = deserializeJson(doc, payload, length);
      if (!err) {
        String t = doc["type"] | "";
        if (t == "control_command") {
          String cmd = doc["command"] | "";
          Serial.printf("[WS] CMD: %s\n", cmd.c_str());
          handleCommand(cmd);
        }
      } else {
        Serial.println("[WS] JSON parse error on text");
      }
      break;
    }

    case WStype_ERROR:
      wsConnected = false;
      Serial.println("[WS] Error");
      break;

    default:
      break;
  }
}

void setup() {
  Serial.begin(115200);

  // Flash LED
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

// Imposta orientamento desiderato (es: mirror orizzontale, nessun flip verticale)
setFlipMirror(0, 1);
  // WiFi: captive portal se non trova credenziali
  WiFiManager wm;
  if (!wm.autoConnect("ESP32_SETUP")) {
    Serial.println("[WiFi] WiFiManager failed, AP attivo");
  }
  Serial.printf("[WiFi] Connesso IP: %s  RSSI: %d\n",
                WiFi.localIP().toString().c_str(), WiFi.RSSI());

  // Camera
  camera_config_t config;
  config.ledc_channel = LEDC_CHANNEL_0;
  config.ledc_timer   = LEDC_TIMER_0;
  config.pin_d0       = Y2_GPIO_NUM;
  config.pin_d1       = Y3_GPIO_NUM;
  config.pin_d2       = Y4_GPIO_NUM;
  config.pin_d3       = Y5_GPIO_NUM;
  config.pin_d4       = Y6_GPIO_NUM;
  config.pin_d5       = Y7_GPIO_NUM;
  config.pin_d6       = Y8_GPIO_NUM;
  config.pin_d7       = Y9_GPIO_NUM;
  config.pin_xclk     = XCLK_GPIO_NUM;
  config.pin_pclk     = PCLK_GPIO_NUM;
  config.pin_vsync    = VSYNC_GPIO_NUM;
  config.pin_href     = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn     = PWDN_GPIO_NUM;
  config.pin_reset    = RESET_GPIO_NUM;
  config.xclk_freq_hz = 20000000;
  config.pixel_format = PIXFORMAT_JPEG;

  if (psramFound()) {
    config.frame_size   = FRAMESIZE_VGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_PSRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;
  } else {
    config.frame_size   = FRAMESIZE_QVGA;
    config.jpeg_quality = 12;
    config.fb_count     = 1;
    config.fb_location  = CAMERA_FB_IN_DRAM;
    config.grab_mode    = CAMERA_GRAB_LATEST;
  }

  if (esp_camera_init(&config) != ESP_OK) {
    Serial.println("[CAM] Init fallita");
    delay(5000);
    ESP.restart();
  }

  // Orientamento corretto (immagine non capovolta, mirror orizzontale se serve)
  sensor_t* s = esp_camera_sensor_get();
  if (s) {
    s->set_vflip(s, 0);      // 0 = non capovolto, 1 = capovolto
    s->set_hmirror(s, 1);    // 1 = mirror, 0 = normale
    s->set_brightness(s, 1);
    s->set_contrast(s, 2);
  }

  // UART GPS su GPIO12 (TX GPS --> 12 ESP32)
  GPSSerial.begin(GPS_BAUD, SERIAL_8N1, GPS_RX, GPS_TX);

  // WebSocket
  webSocket.begin(WS_HOST, WS_PORT, "/");
  webSocket.onEvent(webSocketEvent);
  webSocket.setReconnectInterval(5000);
  Serial.printf("[WS] ws://%s:%u/\n", WS_HOST, WS_PORT);

  Serial.println("SYSTEM READY - PetTracker WS");
}

void loop() {
  webSocket.loop();

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("[WiFi] Persa connessione");
    delay(1000);
    return;
  }

  const uint32_t now = millis();

  // Ricezione continua dati GPS
  while (GPSSerial.available() > 0) {
    gps.encode(GPSSerial.read());
  }

  if (wsConnected) {
    if (now - lastFrameMs >= frameIntervalMs) {
      sendVideoFrame();
      lastFrameMs = now;
    }
    if (now - lastHeartbeatMs >= HEARTBEAT_EVERY_MS) {
      sendHeartbeat();
      lastHeartbeatMs = now;
    }
  }

  if (now - lastSensorReadMs >= SENSOR_READ_EVERY_MS) {
    sendGpsData();
    lastSensorReadMs = now;
  }

  delay(5);
}

void sendHello() {
  DynamicJsonDocument doc(256);
  doc["type"]     = "esp32_hello";
  doc["device"]   = "ESP32-CAM";
  doc["version"]  = "pettracker-ws-1.0";
  doc["ip"]       = WiFi.localIP().toString();
  doc["mac"]      = WiFi.macAddress();
  doc["rssi"]     = WiFi.RSSI();
  doc["uptime"]   = millis();

  String s; serializeJson(doc, s);
  webSocket.sendTXT(s);
}

void sendHeartbeat() {
  DynamicJsonDocument doc(256);
  doc["type"]        = "heartbeat";
  doc["timestamp"]   = millis();
  doc["frames_sent"] = framesSent;
  doc["free_heap"]   = ESP.getFreeHeap();
  doc["wifi_rssi"]   = WiFi.RSSI();

  String s; serializeJson(doc, s);
  webSocket.sendTXT(s);
}

void sendVideoFrame() {
  camera_fb_t* fb = esp_camera_fb_get();
  if (!fb) return;

  if (fb->len > 0 && fb->len <= MAX_JPEG_BYTES) {
    String b64 = base64::encode(fb->buf, fb->len);
    if (b64.length() < MAX_JSON_CHARS) {
      DynamicJsonDocument doc(300);
      doc["type"]      = "frame";
      doc["frame"]     = b64;
      doc["size"]      = fb->len;
      doc["num"]       = framesSent;
      doc["timestamp"] = millis();

      String s;
      serializeJson(doc, s);
      if (webSocket.sendTXT(s)) {
        framesSent++;
        if ((framesSent % 50) == 0) {
          Serial.printf("[CAM] #%u: %u bytes -> %u chars JSON\n",
                        framesSent, fb->len, s.length());
        }
      }
    } else {
      Serial.println("[CAM] JSON troppo grande: riduci qualità/frame_size");
    }
  }
  esp_camera_fb_return(fb);
}

void sendGpsData() {
  if (gps.location.isValid() && gps.location.isUpdated()) {
    DynamicJsonDocument doc(192);
    doc["type"] = "sensor_data";
    doc["gps"]["lat"] = gps.location.lat();
    doc["gps"]["lon"] = gps.location.lng();
    doc["environmental"]["temperature"] = 0; // aggiungi qui altri sensori se presenti
    doc["environmental"]["humidity"] = 0;
    String s; serializeJson(doc, s);
    webSocket.sendTXT(s);

    Serial.print("[GPS] Latitudine: ");
    Serial.println(gps.location.lat(), 6);
    Serial.print("[GPS] Longitudine: ");
    Serial.println(gps.location.lng(), 6);
  } else {
    Serial.println("[GPS] Nessun fix valido");
  }
}

// === Comandi controllo ===
void handleCommand(const String& cmd) {
  if (cmd.startsWith("flash:")) {
    String v = cmd.substring(6);
    bool on = (v == "on" || v == "1" || v == "true");
    digitalWrite(FLASH_LED_PIN, on ? HIGH : LOW);
    Serial.printf("[CMD] Flash %s\n", on ? "ON" : "OFF");
    return;
  }

  if (cmd.startsWith("fps:")) {
    int val = cmd.substring(4).toInt();
    if (val >= 50 && val <= 1000) {
      frameIntervalMs = (uint32_t)val;
      Serial.printf("[CMD] frameInterval=%u ms\n", frameIntervalMs);
    } else {
      Serial.println("[CMD] fps invalido (50..1000 ms)");
    }
    return;
  }

  if (cmd.startsWith("framesize:")) {
    String fs = cmd.substring(10);
    applyFrameSize(fs);
    return;
  }

  if (cmd.startsWith("flip:")) {
    String v = cmd.substring(5);
    setFlipMirror(v == "on", -1);
    return;
  }

  if (cmd.startsWith("mirror:")) {
    String v = cmd.substring(7);
    setFlipMirror(-1, v == "on");
    return;
  }

  Serial.printf("[CMD] Non riconosciuto: %s\n", cmd.c_str());
}

void applyFrameSize(const String& fs) {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return;

  framesize_t target = FRAMESIZE_VGA;
  if      (fs == "qvga") target = FRAMESIZE_QVGA;
  else if (fs == "hvga") target = FRAMESIZE_HVGA;
  else if (fs == "vga")  target = FRAMESIZE_VGA;
  else if (fs == "svga") target = FRAMESIZE_SVGA;
  else if (fs == "xga")  target = FRAMESIZE_XGA;

  s->set_framesize(s, target);
  Serial.printf("[CMD] framesize -> %s\n", fs.c_str());
}

void setFlipMirror(bool flip, bool mirror) {
  sensor_t* s = esp_camera_sensor_get();
  if (!s) return;

  if (flip != -1)   s->set_vflip(s, flip ? 1 : 0);
  if (mirror != -1) s->set_hmirror(s, mirror ? 1 : 0);

  Serial.printf("[CMD] flip=%s mirror=%s\n",
                (flip== -1 ? "KEEP" : (flip ? "ON" : "OFF")),
                (mirror== -1 ? "KEEP" : (mirror ? "ON" : "OFF")));
}