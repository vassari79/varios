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
 *  Optional hunt mode (HUNT_ENABLED in secrets.h): you carry this board, the
 *  collector names one MAC, and the board parks on that MAC's channel and clicks
 *  a buzzer faster the closer you get. This is the one place where logic lives on
 *  the board rather than in the collector, deliberately: homing in on a signal is
 *  a hand-eye loop and needs feedback in well under a second, which a 2 s POST
 *  round-trip to the phone cannot give.
 *
 *  Honest limits: a phone that is off or in airplane mode emits nothing and is
 *  invisible. Randomized MACs make the raw count larger than the phone count.
 *  RSSI zoning is coarse (corner-of-the-room, not a desk). The RF arm says
 *  "something transmitted in the room", never which device. Hunt mode only works
 *  while the target is actually transmitting, and only over WiFi (a cellular-only
 *  phone has no MAC to lock onto).
 *
 *  Board setup (Arduino IDE):
 *    - "esp32 by Espressif" (v3.x), Board: "ESP32S3 Dev Module".
 *    - Library: "NimBLE-Arduino" (2.x).
 *    - USB CDC On Boot: Enabled.
 *
 *  RF wiring (only if RF_ENABLED):  antenna -> AD8317 SMA,
 *  module VOUT -> RF_ADC_PIN, module 3V3 -> 3V3, GND -> GND.
 *  Hunt wiring (only if HUNT_ENABLED): buzzer + -> HUNT_BUZZER_PIN, - -> GND.
 *  HUNT_ZERO_PIN defaults to GPIO0 (the BOOT button), so it needs no wiring.
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
struct Hit { uint64_t mac; int8_t rssi; uint8_t src; uint8_t flags; uint16_t len; uint8_t ch; }; // src:0=WiFi 1=BLE  flags:b0 random b1 apple  ch:0=BLE
struct Dev { int8_t rssiBest; uint32_t lastSeen; uint8_t src; uint8_t flags; uint32_t packets; uint32_t bytes; uint8_t ch; };

static QueueHandle_t          hitQueue;
static std::map<uint64_t,Dev> devices;
static uint32_t               lastPostMs = 0;
static NimBLEScan*            pScan = nullptr;

// ---------------- RF (cellular) arm ----------------
#if RF_ENABLED
static const uint32_t RF_SAMPLE_US = 200;    // ~5 kHz: a 577 us GSM slot spans ~3 samples
static const uint16_t RF_BURST_MV  = 66;     // ~3 dB away from the quiet level = a burst sample
static volatile bool  rfBlank = false;       // true while WE transmit, so our own POST isn't a "burst"
static portMUX_TYPE   rfMux   = portMUX_INITIALIZER_UNLOCKED;
static uint16_t rfMin = 4095, rfMax = 0, rfRef = 0;   // rfRef = last interval's quiet level
static bool     rfHaveRef = false;
static uint32_t rfSum = 0, rfN = 0, rfHits = 0;

// A bare AD8317/AD8318 is inverting: more RF power means LOWER voltage, so the quiet
// level is the interval's highest reading and a burst pushes below it. A module with an
// inverting output stage does the reverse. RF_INVERTED (secrets.h) picks which.
#if RF_INVERTED
  #define RF_IS_BURST(mv, ref) ((mv) + RF_BURST_MV < (ref))
  #define RF_QUIET_OF(mn, mx)  (mx)
#else
  #define RF_IS_BURST(mv, ref) ((mv) > (ref) + RF_BURST_MV)
  #define RF_QUIET_OF(mn, mx)  (mn)
#endif

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
      if (rfHaveRef && RF_IS_BURST(mv, rfRef)) rfHits++;
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
  if (rfN) { rfRef = RF_QUIET_OF(rfMin, rfMax); rfHaveRef = true; }
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

// ---------------- Hunt arm (carried board: park on one MAC, buzz by proximity) ----------------
#if HUNT_ENABLED
static const uint32_t HUNT_POST_MS  = 5000;  // POST this often while hunting. Every POST costs
                                             // ~200 ms of deafness, so do it less than usual.
static const uint32_t HUNT_WIN_MS   = 250;   // peak-hold bucket; two of them = a 250..500 ms window
static const uint32_t HUNT_LOST_MS  = 1500;  // go silent if the target hasn't been heard this long
static const int      HUNT_SPAN_DB  = 30;    // dB above the zero point that maps to fastest clicks
static const uint32_t HUNT_SLOW_MS  = 500;   // click interval at the zero point...
static const uint32_t HUNT_FAST_MS  = 25;    // ...and at HUNT_SPAN_DB above it
static const uint16_t HUNT_CLICK_HZ = 3200;
static const uint16_t HUNT_CLICK_MS = 6;

static uint64_t huntMac = 0;                 // 0 = not hunting
static uint8_t  huntCh  = 0;
// Peak-hold, not an average: multipath digs deep nulls but rarely peaks above the direct
// path, so the max over a short window tracks distance and the mean tracks the fading.
static volatile int8_t   huntPeakA = -128, huntPeakB = -128;   // written from the sniffer callback
static volatile uint32_t huntHeardMs = 0;
static uint32_t huntRotMs = 0, huntClickMs = 0;
static int8_t   huntZero = RSSI_FLOOR;       // "silence" level; the re-zero button raises it

static inline int8_t huntPeak() { return huntPeakA > huntPeakB ? huntPeakA : huntPeakB; }

static void huntBuzz(uint32_t now) {
  int8_t peak = huntPeak();
  if (now - huntRotMs >= HUNT_WIN_MS) {                 // rotate the peak-hold buckets
    huntPeakB = huntPeakA; huntPeakA = -128; huntRotMs = now;
  }
  if (peak == -128 || now - huntHeardMs > HUNT_LOST_MS) return;   // nothing to home in on

  int lvl = peak - huntZero;
  if (lvl < 0) lvl = 0;
  if (lvl > HUNT_SPAN_DB) lvl = HUNT_SPAN_DB;
  uint32_t iv = HUNT_SLOW_MS - (HUNT_SLOW_MS - HUNT_FAST_MS) * lvl / HUNT_SPAN_DB;
  if (now - huntClickMs >= iv) {
    huntClickMs = now;
    tone(HUNT_BUZZER_PIN, HUNT_CLICK_HZ, HUNT_CLICK_MS);
  }
}

// Tap = make the current level the new silence, which is a step attenuator done in
// software: it keeps you on the steep part of the scale as you close in. Hold = undo.
static void huntButton(uint32_t now) {
  static uint32_t downAt = 0;
  static bool     held   = false;
  bool down = (digitalRead(HUNT_ZERO_PIN) == LOW);
  if (down && !downAt) { downAt = now; held = false; }
  if (down && !held && now - downAt >= 1000) {
    huntZero = RSSI_FLOOR; held = true;
    tone(HUNT_BUZZER_PIN, 1200, 120);
    Serial.println("[HUNT] zero reset to full scale");
  }
  if (!down && downAt) {
    if (!held) {
      int8_t peak = huntPeak();
      if (peak != -128) huntZero = peak;
      tone(HUNT_BUZZER_PIN, 2400, 40);
      Serial.printf("[HUNT] zero = %d dBm\n", (int)huntZero);
    }
    downAt = 0;
  }
}

// The collector answers every POST with {"hunt":null} or {"hunt":{"m":"..","c":N}}.
static void huntApply(const String& reply) {
  int i = reply.indexOf("\"m\":\"");
  if (i < 0) {
    if (huntMac) Serial.println("[HUNT] target cleared");
    huntMac = 0; huntCh = 0;
    return;
  }
  int end = reply.indexOf('"', i + 5);
  if (end < 0) return;
  String macs = reply.substring(i + 5, end);
  int j  = reply.indexOf("\"c\":", end);
  int ch = (j >= 0) ? reply.substring(j + 4).toInt() : 0;
  uint64_t m = macFromStr(macs.c_str());
  if (!m || ch < 1 || ch > 14) return;                  // no channel yet = nothing to park on
  if (m != huntMac || (uint8_t)ch != huntCh) {
    huntMac = m; huntCh = (uint8_t)ch;
    huntZero = RSSI_FLOOR;                              // fresh target, full scale
    huntPeakA = huntPeakB = -128;
    huntHeardMs = millis();
    Serial.printf("[HUNT] target %s on channel %d\n", macs.c_str(), ch);
  }
}
#endif

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
  h.ch    = p->rx_ctrl.channel;              // the collector needs it to aim a hunt
#if HUNT_ENABLED
  if (huntMac && h.mac == huntMac) {         // feed the buzzer straight from the callback,
    if (h.rssi > huntPeakA) huntPeakA = h.rssi;   // per frame — the queue is too slow for it
    huntHeardMs = millis();
  }
#endif
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
    h.ch    = 0;   // BLE has no WiFi channel, so a BLE-only MAC cannot be hunted
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
    if (h.ch) dv.ch = h.ch;
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
            ",\"p\":" + kv.second.packets + ",\"b\":" + kv.second.bytes;
    if (kv.second.ch) body += ",\"c\":" + String(kv.second.ch);   // so a hunt knows where to park
    body += "}";
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

  uint32_t tConn = millis();
  if (!connectWiFi()) {
    Serial.println("[NET] WiFi connect failed.");
#if RF_ENABLED
    rfBlank = false;
#endif
    return;
  }
  tConn = millis() - tConn;      // reassociation cost: this is time NOT spent sniffing
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
#if HUNT_ENABLED
  if (code == 200) huntApply(http.getString());   // the reply carries the hunt target
#endif
  http.end();
#if RF_ENABLED
  rfBlank = false;
#endif
  Serial.printf("[POST] board %d, %d devices, HTTP %d, reassoc %lums\n",
                (int)BOARD_ID, n, code, (unsigned long)tConn);
}

#if HUNT_ENABLED
// One hunt cycle: park on the target's channel and drive the buzzer for HUNT_POST_MS,
// then report in (which is also how a "stop hunting" reaches us). The BLE phase and the
// channel sweep are both skipped — 13-channel hopping would leave us deaf to the target
// 92% of the time, which is useless for walking in on it.
static void huntStep() {
  if (WiFi.status() == WL_CONNECTED) WiFi.disconnect(false, false);  // else the AP owns the channel
  esp_wifi_set_promiscuous(true);
  esp_wifi_set_channel(huntCh, WIFI_SECOND_CHAN_NONE);
  uint32_t t0 = millis();
  while (millis() - t0 < HUNT_POST_MS && huntMac) {
    drainQueue();
    uint32_t now = millis();
    huntBuzz(now);
    huntButton(now);
    delay(2);
  }
  esp_wifi_set_promiscuous(false);
  noTone(HUNT_BUZZER_PIN);
  postBatch();
  lastPostMs = millis();
}
#endif

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

#if HUNT_ENABLED
  pinMode(HUNT_BUZZER_PIN, OUTPUT);
  pinMode(HUNT_ZERO_PIN, INPUT_PULLUP);
  Serial.printf("Hunt arm: buzzer GPIO%d, re-zero button GPIO%d\n",
                (int)HUNT_BUZZER_PIN, (int)HUNT_ZERO_PIN);
#endif

  lastPostMs = millis();
}

void loop() {
#if HUNT_ENABLED
  if (huntMac) { huntStep(); return; }        // hunting: no sweep, no BLE, just the target
#endif

  // ---- WiFi phase ----
  // The driver pins the radio to the AP's channel for as long as the station is
  // associated, so esp_wifi_set_channel() below is accepted and then silently
  // ignored: the sweep never leaves that one channel and every other channel in
  // the room is invisible. Drop the association before sniffing; postBatch()
  // reconnects to report. Without this the board only ever hops once, in the
  // window between boot and the first POST.
  if (WiFi.status() == WL_CONNECTED) WiFi.disconnect(false, false);
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
