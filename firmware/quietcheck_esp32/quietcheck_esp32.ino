/*
 * QuietCheck — ESP32 "battery black box"
 * One real home battery on the dashboard, next to 500 simulated ones.
 *
 * Hardware
 *   Potentiometer : outer legs to 3V3 and GND, wiper to GPIO 34  -> discharge power 0..5 kW
 *   TRIP button   : GPIO 25 to GND  (short press = trip / reset, hold 2 s = cell overheats)
 *   WIFI button   : GPIO 26 to GND  (toggle "Wi-Fi lost": keeps logging, uploads backlog on return)
 *   LED           : GPIO 27 -> 220 ohm -> LED -> GND   (on-board LED on GPIO 2 mirrors it)
 *
 * LED meaning
 *   solid          running and checking in
 *   slow blink     offline, logging to the local black box (store and forward)
 *   fast blink     tripped (press TRIP again = homeowner reset)
 *   double blink   overheating / failed
 *
 * Messages (JSON over HTTP POST, built with snprintf, no extra libraries)
 *   /api/checkin  every CHECKIN_MS
 *   /api/checkin  with "last_gasp":true and a fault code, once, as it goes down
 *   /api/backlog  records logged while offline, sent on reconnect
 *
 * Board: "ESP32 Dev Module" (Arduino core for ESP32). Serial monitor at 115200.
 */
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>

// ======================= EDIT THESE THREE =======================
const char* WIFI_SSID   = "YOUR_HOTSPOT_NAME";        // 2.4 GHz only (iPhone: turn on "Maximize Compatibility")
const char* WIFI_PASS   = "YOUR_HOTSPOT_PASSWORD";
const char* SERVER_URL  = "https://quietcheck.onrender.com"; // live server (Render)
// =================================================================

const char* DEVICE_ID   = "ESP32-01";
const float LAT = 34.0205, LON = -118.2856;            // shows up at USC on the map

const int PIN_POT = 34, PIN_TRIP = 25, PIN_WIFI = 26, PIN_LED = 27, PIN_LED_BOARD = 2;
const unsigned long CHECKIN_MS = 1000;                 // twice per server tick, so jitter never looks like silence
const float CAPACITY_KWH = 13.5, MAX_KW = 5.0;

// Server link can be changed at runtime: type it into the Serial Monitor and press Enter.
// It is saved in flash (NVS) and survives resets, so a new tunnel link needs no re-upload.
Preferences prefs;
String serverUrl;

// ---------------- black box: circular buffer (same idea as the car black box EEPROM log)
struct Record { unsigned long ms; float soc, kw, temp, volt; char mode[10]; char fault[12]; };
const int BOX_LEN = 60;
Record box[BOX_LEN];
int boxHead = 0, boxCount = 0;      // all records
int pendingStart = -1, pendingCount = 0;  // records logged while offline, not yet uploaded

// ---------------- state
enum BatState { ST_RUNNING, ST_TRIPPED, ST_OVERHEATING, ST_FAILED };
BatState state = ST_RUNNING;
bool wifiLost = false;              // the WIFI button simulates the home router dying
float soc = 82.0, kw = 0, temp = 27.0, volt = 400.0;
unsigned long lastTick = 0, tripDownAt = 0;
bool tripWasDown = false, wifiWasDown = false, overheatFired = false;

// ============================================================ helpers
bool post(const char* path, const String& body) {
  if (WiFi.status() != WL_CONNECTED) return false;
  String url = serverUrl + path;
  HTTPClient http;
  WiFiClientSecure secure;
  WiFiClient plain;
  bool ok;
  if (url.startsWith("https")) { secure.setInsecure(); ok = http.begin(secure, url); }
  else                         { ok = http.begin(plain, url); }
  if (!ok) return false;
  http.setTimeout(url.startsWith("https") ? 6000 : 1500);  // TLS to Render needs longer
  http.addHeader("Content-Type", "application/json");
  int code = http.POST(body);
  http.end();
  if (code != 200) Serial.printf("POST %s -> %d\n", path, code);
  return code == 200;
}

String recordJson(const Record& r, bool lastGasp, unsigned long nowMs, bool withAge) {
  char buf[320];
  char fault[20];
  if (r.fault[0]) snprintf(fault, sizeof(fault), "\"%s\"", r.fault); else strcpy(fault, "null");
  int n = snprintf(buf, sizeof(buf),
    "{\"id\":\"%s\",\"soc\":%.1f,\"power_kw\":%.2f,\"mode\":\"%s\",\"temp_c\":%.1f,"
    "\"voltage\":%.1f,\"fault\":%s,\"lat\":%.5f,\"lon\":%.5f%s",
    DEVICE_ID, r.soc, r.kw, r.mode, r.temp, r.volt, fault, LAT, LON,
    lastGasp ? ",\"last_gasp\":true" : "");
  if (withAge) n += snprintf(buf + n, sizeof(buf) - n, ",\"age_s\":%.1f", (nowMs - r.ms) / 1000.0);
  snprintf(buf + n, sizeof(buf) - n, "}");
  return String(buf);
}

Record makeRecord(const char* fault) {
  Record r;
  r.ms = millis(); r.soc = soc; r.kw = kw; r.temp = temp; r.volt = volt;
  strcpy(r.mode, kw > 0.05 ? "DISCHARGE" : (kw < -0.05 ? "CHARGE" : "IDLE"));
  strncpy(r.fault, fault ? fault : "", sizeof(r.fault) - 1); r.fault[sizeof(r.fault) - 1] = 0;
  return r;
}

void logRecord(const Record& r, bool offline) {
  box[boxHead] = r;
  if (offline) {
    if (pendingCount == 0) pendingStart = boxHead;
    if (pendingCount < BOX_LEN) pendingCount++;
    else pendingStart = (pendingStart + 1) % BOX_LEN;   // oldest pending record overwritten
  }
  boxHead = (boxHead + 1) % BOX_LEN;
  if (boxCount < BOX_LEN) boxCount++;
}

void uploadBacklog() {
  if (pendingCount == 0) return;
  unsigned long now = millis();
  String body = String("{\"id\":\"") + DEVICE_ID + "\",\"records\":[";
  for (int i = 0; i < pendingCount; i++) {
    const Record& r = box[(pendingStart + i) % BOX_LEN];
    if (i) body += ",";
    body += recordJson(r, false, now, true);
  }
  body += "]}";
  if (post("/api/backlog", body)) {
    Serial.printf("Backlog uploaded: %d records\n", pendingCount);
    pendingCount = 0; pendingStart = -1;
  }
}

void sendLastGasp(const char* fault) {
  Record r = makeRecord(fault);
  r.kw = 0; strcpy(r.mode, "IDLE");
  logRecord(r, false);
  // a last gasp only gets out if the network is up — like a real brown-out
  if (!wifiLost) {
    bool ok = post("/api/checkin", recordJson(r, true, millis(), false));
    Serial.printf("LAST GASP %s %s\n", fault, ok ? "sent" : "FAILED");
  }
}

// ============================================================ physics
void updatePhysics(float dtS) {
  if (state == ST_RUNNING || state == ST_OVERHEATING) {
    int raw = analogRead(PIN_POT);                      // 0..4095
    kw = round((raw / 4095.0) * MAX_KW * 10) / 10.0;
    if (soc <= 10) kw = 0;
  } else {
    kw = 0;
  }
  // 1 real second stands in for 30 simulated seconds (same scale as the server), so the charge visibly moves
  soc -= kw * (dtS * 30 / 3600.0) / CAPACITY_KWH * 100;
  if (soc < 0) soc = 0;

  float target = 26 + 1.2 * kw;
  if (state == ST_OVERHEATING) target = temp + 4;          // climbs ~4 C per second
  temp += (target - temp) * (state == ST_OVERHEATING ? 1.0 : 0.3);
  volt = 400 - 1.5 * kw - (state == ST_OVERHEATING ? (temp - 30) * 1.2 : 0);
}

// ============================================================ inputs
void readButtons() {
  bool tripDown = digitalRead(PIN_TRIP) == LOW;
  bool wifiDown = digitalRead(PIN_WIFI) == LOW;
  unsigned long now = millis();

  // TRIP: short press toggles trip/reset, hold 2 s starts overheating
  if (tripDown && !tripWasDown) tripDownAt = now;
  if (tripDown && state == ST_RUNNING && now - tripDownAt > 2000) {
    state = ST_OVERHEATING; overheatFired = false;
    Serial.println(">> Cell overheating (black box will show the climb)");
  }
  if (!tripDown && tripWasDown && now - tripDownAt < 2000 && now - tripDownAt > 40) {
    if (state == ST_RUNNING) {
      state = ST_TRIPPED;
      Serial.println(">> TRIPPED");
      sendLastGasp("TRIP");
    } else {
      state = ST_RUNNING; temp = 30; overheatFired = false;
      Serial.println(">> Reset by homeowner, running again");
    }
  }
  tripWasDown = tripDown;

  // WIFI: toggle simulated router loss
  if (wifiDown && !wifiWasDown) {
    wifiLost = !wifiLost;
    Serial.println(wifiLost ? ">> Wi-Fi LOST (logging locally)" : ">> Wi-Fi BACK (uploading backlog)");
    delay(40);
  }
  wifiWasDown = wifiDown;
}

void updateLed() {
  unsigned long t = millis();
  bool on;
  if (state == ST_TRIPPED)                           on = (t / 120) % 2;
  else if (state == ST_OVERHEATING || state == ST_FAILED) on = (t % 1000) < 120 || ((t % 1000) > 240 && (t % 1000) < 360);
  else if (wifiLost || WiFi.status() != WL_CONNECTED) on = (t / 600) % 2;
  else                                             on = true;
  digitalWrite(PIN_LED, on); digitalWrite(PIN_LED_BOARD, on);
}

// ============================================================ setup / loop
const char* authName(int a) {
  switch (a) { case 0: return "open"; case 1: return "WEP"; case 2: return "WPA"; case 3: return "WPA2";
               case 4: return "WPA/WPA2"; case 6: return "WPA3"; case 7: return "WPA2/WPA3"; default: return "other"; }
}

// Why a connection attempt failed (ESP-IDF reason codes)
void onWifiEvent(WiFiEvent_t event, WiFiEventInfo_t info) {
  if (event == ARDUINO_EVENT_WIFI_STA_DISCONNECTED) {
    int r = info.wifi_sta_disconnected.reason;
    const char* why = (r == 201) ? "network not found (5 GHz only? name typo?)"
                    : (r == 15 || r == 204 || r == 2 || r == 202) ? "wrong password or WPA3-only security"
                    : (r == 203) ? "association refused (hotspot full or blocking)"
                    : "see ESP-IDF reason list";
    Serial.printf("Wi-Fi attempt failed, reason %d: %s\n", r, why);
  } else if (event == ARDUINO_EVENT_WIFI_STA_GOT_IP) {
    Serial.printf("Connected, IP %s -> %s\n", WiFi.localIP().toString().c_str(), serverUrl.c_str());
  }
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.onEvent(onWifiEvent);

  // Diagnostic: list every 2.4 GHz network the ESP32 can actually see
  Serial.println("Scanning for networks...");
  int n = WiFi.scanNetworks();
  bool seen = false;
  for (int i = 0; i < n; i++) {
    bool match = WiFi.SSID(i) == String(WIFI_SSID);
    seen |= match;
    Serial.printf("  %s %-28s ch%-2d %4d dBm  %s\n", match ? ">>" : "  ", WiFi.SSID(i).c_str(),
                  WiFi.channel(i), WiFi.RSSI(i), authName(WiFi.encryptionType(i)));
  }
  Serial.println(seen ? "Target network FOUND" : "Target network NOT visible on 2.4 GHz");
  WiFi.scanDelete();

  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("Connecting to \"%s\"", WIFI_SSID);
  for (int i = 0; i < 60 && WiFi.status() != WL_CONNECTED; i++) { delay(250); Serial.print("."); updateLed(); }
  Serial.println();
  if (WiFi.status() != WL_CONNECTED) Serial.println("Wi-Fi not connected yet, will keep trying");
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_TRIP, INPUT_PULLUP);
  pinMode(PIN_WIFI, INPUT_PULLUP);
  pinMode(PIN_LED, OUTPUT); pinMode(PIN_LED_BOARD, OUTPUT);
  analogReadResolution(12);
  prefs.begin("quietcheck", false);
  serverUrl = prefs.getString("url", SERVER_URL);
  Serial.printf("Server: %s   (type a new https://... link + Enter to change it)\n", serverUrl.c_str());
  connectWifi();
  lastTick = millis();
}

void readSerialCommand() {
  if (!Serial.available()) return;
  String line = Serial.readStringUntil('\n');
  line.trim();
  while (line.endsWith("/")) line.remove(line.length() - 1);
  if (line.startsWith("http")) {
    serverUrl = line;
    prefs.putString("url", serverUrl);
    Serial.printf(">> Server changed to %s (saved)\n", serverUrl.c_str());
  } else if (line == "url") {
    Serial.printf(">> Server is %s\n", serverUrl.c_str());
  } else if (line == "reset-url") {
    prefs.remove("url"); serverUrl = SERVER_URL;
    Serial.printf(">> Server reset to %s\n", serverUrl.c_str());
  }
}

void loop() {
  readSerialCommand();
  readButtons();
  updateLed();

  unsigned long now = millis();
  if (now - lastTick < CHECKIN_MS) { delay(10); return; }
  float dt = (now - lastTick) / 1000.0;
  lastTick = now;

  static unsigned long lastReconnect = 0;
  // retry slowly: reconnecting too often aborts an attempt that is still in progress
  if (WiFi.status() != WL_CONNECTED && now - lastReconnect > 20000) {
    lastReconnect = now; WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASS);
  }
  updatePhysics(dt);

  // overheating ends in a failure with a last gasp
  if (state == ST_OVERHEATING && temp >= 70 && !overheatFired) {
    overheatFired = true; state = ST_FAILED;
    Serial.println(">> Cell FAILED");
    sendLastGasp("OVERTEMP");
  }
  if (state == ST_TRIPPED || state == ST_FAILED) return;    // a dead battery says nothing

  Record r = makeRecord(nullptr);
  bool offline = wifiLost || WiFi.status() != WL_CONNECTED;
  if (offline) {
    logRecord(r, true);
    Serial.printf("[offline] logged locally (%d waiting) kW=%.1f SoC=%.1f\n", pendingCount, r.kw, r.soc);
    return;
  }
  uploadBacklog();                                    // store and forward first
  logRecord(r, false);
  bool ok = post("/api/checkin", recordJson(r, false, now, false));
  Serial.printf("check-in %s kW=%.1f SoC=%.1f T=%.1f V=%.0f\n", ok ? "ok " : "ERR", r.kw, r.soc, r.temp, r.volt);
}
