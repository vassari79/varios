/*  signal_detector.ino  ---------------------------------------------------------
 *  ESP32-S3 "dumb sensor" for room-level 2.4 GHz device presence.
 *
 *  It sniffs WiFi (promiscuous, hopping ch 1/6/11) and BLE advertisements,
 *  then every POST_INTERVAL_MS it POSTs everything it heard as JSON to the
 *  collector (collector.py running in Termux on your phone). All the logic —
 *  baseline, threshold, cross-board zoning, alerts — lives in the collector.
 *
 *  Flash two boards with a different BOARD_ID (1 and 2) in secrets.h and place
 *  them at opposite corners; the collector compares each MAC's RSSI across the
 *  two to zone a device to a rough part of the room.
 *
 *  Optional third arm (RF_ENABLED in secrets.h): an AD8317/AD8318 log power
 *  detector on an ADC pin. That covers what the 2.4 GHz radio cannot see — a
 *  phone transmitting on a cellular band. It is broadband (no filter), so the
 *  board reports raw millivolts and the collector decides what is a burst.
 *
 *  Honest limits: a phone that is off or in airplane mode emits nothing and is
 *  invisible. Randomized MACs make the raw count larger than the phone count.
 *  RSSI zoning is coarse (corner-of-the-room, not a desk). The RF arm says
 *  "something transmitted in the room", never which device.
 *
 *  Board setup (Arduino IDE):
 *    - "esp32 by Espressif" (v3.x), Board: "ESP32S3 Dev Module".
 *    - Library: "NimBLE-Arduino" (2.x).
 *    - USB CDC On Boot: Enabled.
 *
 *  RF wiring (only if RF_ENABLED):  antenna -> AD8317 SMA,
 *  module VOUT -> RF_ADC_PIN, module 3V3 -> 3V3, GND -> GND.
 * ---------------------------------------------------------------------------*/

#include <Arduino.h>
#include <WiFi.h>
#include "esp_wifi.h"
#include <HTTPClient.h>
#include <NimBLEDevice.h>
#include <set>
#include <map>
#include "secrets.h"   // WIFI_SSID/PASS, BOARD_ID, SERVER_HOST, SERVER_PORT (git-ignored)

// ---------------- Config ----------------
static const int8_t   RSSI_FLOOR      = -90;            // drop hits weaker than this (server decides the real threshold)
static const uint8_t  WIFI_CHANNELS[] = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13};
static const uint32_t WIFI_DWELL_MS   = 80;    // per channel; 13 ch x 80ms ~= 1.0s/sweep
static const uint32_t BLE_WINDOW_MS   = 900;
static const uint32_t EXPIRE_MS       = 120000;         // forget a MAC unseen this long
static const uint32_t POST_INTERVAL_MS= 2000;           // how often to ship a batch (~= sniff cycle)
static const int      MAX_DEVS_PER_POST = 250;

// ---------------- State ----------------
struct Hit { uint64_t mac; int8_t rssi; uint8_t src; uint8_t flags; uint16_t len; }; // src:0=WiFi 1=BLE  flags:b0 random b1 apple
struct Dev { int8_t rssiBest; uint32_t lastSeen; uint8_t src; uint8_t flags; uint32_t packets; uint32_t bytes; };

static QueueHandle_t          hitQueue;
static std::map<uint64_t,Dev> devices;
static uint32_t               lastPostMs = 0;
static NimBLEScan*            pScan = nullptr;

// ---------------- RF (cellular) arm ----------------
#if RF_ENABLED
static const uint32_t RF_SAMPLE_US = 200;    // ~5 kHz: a 577 us GSM slot spans ~3 samples
static const uint16_t RF_BURST_MV  = 66;     // ~3 dB below the quiet level counts as a burst sample
                                             // (AD8317 is inverting: more RF power = LOWER voltage)
static volatile bool  rfBlank = false;       // true while WE transmit, so our own POST isn't a "burst"
static portMUX_TYPE   rfMux   = portMUX_INITIALIZER_UNLOCKED;
static uint16_t rfMin = 4095, rfMax = 0, rfRef = 0;   // rfRef = last interval's quiet level
static uint32_t rfSum = 0, rfN = 0, rfHits = 0;

static void rfTask(void*) {
  uint32_t k = 0;
  for (;;) {
    if (!rfBlank) {
      uint16_t mv = analogReadMilliVolts(RF_ADC_PIN);
      portENTER_CRITICAL(&rfMux);
      if (mv < rfMin) rfMin = mv;
      if (mv > rfMax) rfMax = mv;
      rfSum += mv;
      rfN++;
      if (rfRef && mv + RF_BURST_MV < rfRef) rfHits++;
      portEXIT_CRITICAL(&rfMux);
    }
    delayMicroseconds(RF_SAMPLE_US);
    if (++k % 500 == 0) vTaskDelay(1);       // feed the watchdog / let others run
  }
}

// Take and reset the interval's stats. The interval's quiet level (max mV) becomes
// the reference the next interval counts burst samples against.
static void rfSnapshot(uint16_t& mn, uint16_t& mx, uint16_t& avg, uint32_t& n, uint32_t& hits) {
  portENTER_CRITICAL(&rfMux);
  mn = rfMin; mx = rfMax; n = rfN; hits = rfHits;
  avg = rfN ? (uint16_t)(rfSum / rfN) : 0;
  if (rfMax) rfRef = rfMax;
  rfMin = 4095; rfMax = 0; rfSum = 0; rfN = 0; rfHits = 0;
  portEXIT_CRITICAL(&rfMux);
}
#endif

// ---------------- Helpers ----------------
static uint64_t macFromStr(const std::string& s) {
  unsigned v[6] = {0};
  if (sscanf(s.c_str(), "%x:%x:%x:%x:%x:%x", &v[0],&v[1],&v[2],&v[3],&v[4],&v[5]) == 6) {
    uint64_t m = 0;
    for (int i = 0; i < 6; i++) m = (m << 8) | (v[i] & 0xFF);
    return m;
  }
  return 0;
}

static String macToStr(uint64_t m) {
  char b[18];
  snprintf(b, sizeof(b), "%02X:%02X:%02X:%02X:%02X:%02X",
    (uint8_t)(m >> 40), (uint8_t)(m >> 32), (uint8_t)(m >> 24),
    (uint8_t)(m >> 16), (uint8_t)(m >> 8),  (uint8_t)m);
  return String(b);
}

static bool connectWiFi(uint32_t timeoutMs = 10000) {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  uint32_t t0 = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - t0 < timeoutMs) delay(200);
  return WiFi.status() == WL_CONNECTED;
}

// ---------------- Sniffer callbacks ----------------
static void wifiSniffer(void* buf, wifi_promiscuous_pkt_type_t type) {
  if (type == WIFI_PKT_MISC) return;
  const wifi_promiscuous_pkt_t* p = (const wifi_promiscuous_pkt_t*)buf;
  if (p->rx_ctrl.sig_len < 16) return;
  const uint8_t* d = p->payload;
  uint8_t ftype = (d[0] >> 2) & 0x03;
  if (ftype == 0x01) return;                 // control frames may lack addr2
  const uint8_t* a2 = d + 10;
  if (a2[0] & 0x01) return;                  // group/broadcast

  Hit h;
  h.mac = 0;
  for (int i = 0; i < 6; i++) h.mac = (h.mac << 8) | a2[i];
  h.rssi  = p->rx_ctrl.rssi;
  h.src   = 0;
  h.flags = (a2[0] & 0x02) ? 0x01 : 0x00;    // locally-administered = randomized
  h.len   = p->rx_ctrl.sig_len;              // frame length (header + payload + FCS)
  xQueueSend(hitQueue, &h, 0);
}

class ScanCB : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* dev) override {
    Hit h;
    h.mac   = macFromStr(dev->getAddress().toString());
    h.rssi  = dev->getRSSI();
    h.src   = 1;
    h.flags = 0;
    h.len   = 0;   // BLE advertisement volume isn't a useful "traffic" proxy
    if (dev->getAddress().getType() != BLE_ADDR_PUBLIC) h.flags |= 0x01;
    if (dev->haveManufacturerData()) {
      std::string md = dev->getManufacturerData();
      if (md.size() >= 2 && (uint8_t)md[0] == 0x4C && (uint8_t)md[1] == 0x00) h.flags |= 0x02;
    }
    xQueueSend(hitQueue, &h, 0);
  }
};
static ScanCB scanCB;

// ---------------- Queue drain ----------------
static void drainQueue() {
  Hit h;
  while (xQueueReceive(hitQueue, &h, 0) == pdTRUE) {
    if (h.rssi < RSSI_FLOOR) continue;
    Dev& dv = devices[h.mac];
    if (dv.lastSeen == 0 || h.rssi > dv.rssiBest) dv.rssiBest = h.rssi;
    dv.lastSeen = millis();
    dv.src      = h.src;
    dv.flags   |= h.flags;
    dv.packets += 1;
    dv.bytes   += h.len;
  }
}

// ---------------- POST batch ----------------
static void postBatch() {
  uint32_t now = millis();
  for (auto it = devices.begin(); it != devices.end();) {          // expire first
    if (now - it->second.lastSeen > EXPIRE_MS) it = devices.erase(it);
    else ++it;
  }

  String body = String("{\"board\":") + BOARD_ID + ",\"devs\":[";
  bool first = true; int n = 0;
  for (auto& kv : devices) {
    if (n >= MAX_DEVS_PER_POST) break;
    if (!first) body += ',';
    first = false;
    body += "{\"m\":\"" + macToStr(kv.first) + "\",\"r\":" + kv.second.rssiBest +
            ",\"s\":" + kv.second.src + ",\"f\":" + kv.second.flags +
            ",\"p\":" + kv.second.packets + ",\"b\":" + kv.second.bytes + "}";
    kv.second.packets = 0;   // reset so each POST reports traffic since the last one
    kv.second.bytes   = 0;
    n++;
  }
  body += "]";

#if RF_ENABLED
  uint16_t mn, mx, avg; uint32_t rn, hits;
  rfSnapshot(mn, mx, avg, rn, hits);
  if (rn) {
    body += ",\"rf\":{\"n\":" + String(rn) + ",\"min\":" + mn + ",\"max\":" + mx +
            ",\"avg\":" + avg + ",\"hits\":" + String(hits) + "}";
  }
  rfBlank = true;                 // our own 2.4 GHz TX would swamp a broadband detector
#endif
  body += "}";

  if (!connectWiFi()) {
    Serial.println("[NET] WiFi connect failed.");
#if RF_ENABLED
    rfBlank = false;
#endif
    return;
  }
  WiFiClient client;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + SERVER_PORT + "/report";
  if (!http.begin(client, url)) {
    Serial.println("[NET] begin failed.");
#if RF_ENABLED
    rfBlank = false;
#endif
    return;
  }
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
#if RF_ENABLED
  rfBlank = false;
#endif
  Serial.printf("[POST] board %d, %d devices, HTTP %d\n", (int)BOARD_ID, n, code);
}

// ---------------- Setup / loop ----------------
void setup() {
  Serial.begin(115200);
  delay(600);
  Serial.printf("\nsignal_detector sensor — board %d\n", (int)BOARD_ID);

  hitQueue = xQueueCreate(256, sizeof(Hit));

  WiFi.mode(WIFI_STA);
  WiFi.disconnect(true, true);
  esp_wifi_set_promiscuous(false);
  esp_wifi_set_promiscuous_rx_cb(&wifiSniffer);

  NimBLEDevice::init("");
  pScan = NimBLEDevice::getScan();
  pScan->setScanCallbacks(&scanCB, false);
  pScan->setActiveScan(false);
  pScan->setInterval(45);
  pScan->setWindow(45);

#if RF_ENABLED
  analogSetPinAttenuation(RF_ADC_PIN, ADC_11db);   // full ~0..3.1 V span; AD8317 tops out near 2 V
  xTaskCreatePinnedToCore(rfTask, "rf", 2048, nullptr, 1, nullptr, 1);
  Serial.printf("RF arm on GPIO%d\n", (int)RF_ADC_PIN);
#endif

  lastPostMs = millis();
}

void loop() {
  // ---- WiFi phase ----
  esp_wifi_set_promiscuous(true);
  for (uint8_t ch : WIFI_CHANNELS) {
    esp_wifi_set_channel(ch, WIFI_SECOND_CHAN_NONE);
    uint32_t t = millis();
    while (millis() - t < WIFI_DWELL_MS) { drainQueue(); delay(5); }
  }
  esp_wifi_set_promiscuous(false);

  // ---- BLE phase ----
  pScan->start(0, false);
  uint32_t t = millis();
  while (millis() - t < BLE_WINDOW_MS) { drainQueue(); delay(5); }
  pScan->stop();
  drainQueue();

  if (millis() - lastPostMs >= POST_INTERVAL_MS) {
    postBatch();
    lastPostMs = millis();
  }
}
