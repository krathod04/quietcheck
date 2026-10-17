# QuietCheck — a black box for every home battery

**Energy & Grid lens (Base Power) · InnovateLA Dual-Use Hackathon 2026**

> In a disaster, silence is the most dangerous data. QuietCheck turns it into a decision.

When a home battery in a virtual power plant stops checking in, the dispatcher can't tell if it
**broke**, **tripped**, or is **just not talking**. A naive system either trusts only the batteries it can
hear (and under-counts capacity) or sends a truck to every silent battery. QuietCheck cross-checks every
silent battery against the signals that are still talking — smart meters, neighbors on the same
feeder, batteries on the same cellular carrier, the battery's own last gasp and its black-box log —
and picks the **gentlest response that still works**:

`WATCH → PING → ASK (homeowner) → HOLD (capacity) → DISPATCH (technician)`

| Battery | Smart meter | Neighbors | Diagnosis | Response |
|---|---|---|---|---|
| Silent | Normal, home load unchanged | Fine | **Comms loss** | Ping fallback path, keep capacity, no truck |
| Silent | Home suddenly pulling from grid | Fine | **Tripped** | Text/call homeowner reset steps |
| Silent, black box shows heat / voltage sag | — | — | **Hardware failing** | Hold, send tech with parts |
| Silent | Silent | Whole feeder dark | **Grid outage** | Hand to utility, battery is islanding the home |
| Many silent | Fine | Same carrier | **Carrier outage** | Wait it out, keep capacity |
| Talking, heating up | — | — | **At risk** (pre-emptive) | Derate to 50%, book inspection |
| Talking, numbers break physics | Disagrees | — | **Talking but wrong** | Distrust, remove from capacity, security review |

**Dual use:** on an ordinary Tuesday the same black box does predictive maintenance (catch a failing
cell months early, fix it on a scheduled visit) and cuts wasted truck rolls for the installer.

## Run it (2 minutes)

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python server.py
```

Open **http://localhost:8000**. The map uses online tiles; if the venue Wi-Fi is down it falls back
to a built-in offline map automatically.

Settings (environment variables): `TICK_SECONDS` (default 2), `N_BATTERIES` (default 500), `PORT` (8000).

Tests: `python -m unittest discover -s tests -t .` (every fault type, debounce, carrier outage
capacity, mixed-scenario accuracy, real-device path).

## Demo script (3 minutes, working thing first)

1. **Drop carrier → Verizon.** ~200 batteries go purple at once. Naive capacity falls ~45% and a naive
   system would roll 200+ trucks. QuietCheck: "carrier outage, their meters still report", capacity
   stays on the truth line, **0 trucks**. Point at the chart.
2. **Trip battery.** Last gasp arrives → *Tripped, 96%* → homeowner texted → "pressed reset" →
   back online, **truck avoided**.
3. **Cell overheating.** Turns pink (*at risk*) **while still talking** — that's the pre-emptive save.
   Then it dies → black box shows 30 → 51 °C → *Hardware failure* → the only truck of the night,
   with parts.
4. **Black out feeder.** Whole neighborhood gray: grid outage, batteries are keeping homes lit.
5. **Spoof telemetry.** Battery claims 5 kW discharge but its charge never moves → *Talking but wrong*.
6. Click any home to open its **black box**. Accuracy card: % of abnormal batteries named correctly
   vs. simulator ground truth.

## Architecture

```
simulator (500 batteries, meters,      engine (sees only what a real
feeders, carriers, ground truth)  ──►  dispatcher sees) ──► FastAPI ──► dashboard
           ▲                                 ▲                         (polls 1/s)
   fault injection API             real ESP32 check-ins
```

* `quietcheck/sim.py` — fleet simulator; knows the truth, never shares it with the engine.
* `quietcheck/engine.py` — diagnosis rules, confidence, reasons, ladder, capacity estimate.
* `quietcheck/fleet.py` — wires them together; scores the engine against truth.
* `server.py` — FastAPI app + background ticker.
* `static/index.html` — dashboard (Leaflet map, decision feed, black-box viewer, capacity chart).

Debounce works like CAN error counters: one dropped check-in never raises an alarm; watching starts
after 1 missed, diagnosis after 2 consecutive misses.

## ESP32 hardware black box

Firmware: `firmware/quietcheck_esp32/quietcheck_esp32.ino` (Arduino IDE, board "ESP32 Dev Module",
no extra libraries). Edit `WIFI_SSID`, `WIFI_PASS`, `SERVER_URL` at the top, then upload.

| Part | ESP32 pin |
|---|---|
| Potentiometer wiper (outer legs to 3V3 and GND) | GPIO 34 — discharge power 0–5 kW |
| TRIP button (other leg to GND) | GPIO 25 — tap = trip / homeowner reset, hold 2 s = cell overheats |
| WIFI button (other leg to GND) | GPIO 26 — toggle "router died" (logs locally, uploads backlog on return) |
| LED + 220 Ω to GND | GPIO 27 (on-board LED on GPIO 2 mirrors it) |

LED: solid = checking in · slow blink = offline, logging locally · fast blink = tripped · double blink = overheating/failed.

No hardware yet? `python tools/fake_esp32.py` sends the exact same messages from your laptop
(keys: `t` trip/reset, `w` Wi-Fi, `h` overheat, `0`–`5` kW).

Hardware demo: WIFI button → *comms loss, no truck* → WIFI again → backlog uploads, "it was only a
comms loss". Tap TRIP → last gasp → *tripped* → homeowner texted → tap TRIP again (you are the
homeowner) → *truck avoided*. Hold TRIP 2 s → temperature climbs → *at risk* while still talking →
OVERTEMP last gasp → *hardware failure*, the only truck sent.

## Connect a real device (ESP32 black box)

Send a check-in **every 1 second** (twice per tick, so Wi-Fi jitter never looks like silence):

```http
POST /api/checkin
{"id":"ESP32-01","soc":81.5,"power_kw":2.1,"mode":"DISCHARGE","temp_c":31.2,"voltage":398.4,
 "fault":null,"lat":34.0205,"lon":-118.2856}
```

* **Last gasp** (trip button / brown-out): same body with `"last_gasp": true, "fault": "TRIP"`
  (`OVERTEMP`, `CELL_FAULT`, `BMS_FAULT` are read as hardware failure).
* **Store and forward** after Wi-Fi returns: `POST /api/backlog {"id":"ESP32-01","records":[...]}` —
  the dashboard confirms "ran normally the whole time — it was only a comms loss".
* The server gives each real device a **simulated smart meter** (it assumes the battery keeps
  running unless it sent a last gasp), so unplugging Wi-Fi reads as *comms loss* and pressing trip
  reads as *tripped*. Real meter readings can be posted to `POST /api/meter {"id":..,"import_kw":..}`.

Other endpoints: `GET /api/state`, `GET /api/battery/{id}`, `POST /api/inject {"kind","target"}`
(`comms_loss`, `trip`, `trip_silent`, `hw_fail`, `spoof`, `feeder_outage`, `carrier_outage`,
`heatwave`, `restore`), `POST /api/pause`, `POST /api/step`, `POST /api/reset`.

## Responsible use

* **Misuse risk:** the black-box log reveals when a household uses power, which says a lot about
  when people are home. **Mitigation:** logs stay with the fleet operator for fault diagnosis only,
  are kept for a short window, and the dashboard shows device state, not household behavior.
* **Misuse risk:** an attacker could spoof telemetry to fake (or hide) capacity. **Mitigation:** the
  physics/meter cross-check flags "talking but wrong" units and removes them from capacity, and
  homeowner contact and dispatch always stay with a human.

## Built with

Python, FastAPI, Uvicorn, Leaflet, OpenStreetMap/CARTO tiles. AI tools used: Claude (code
scaffolding) — list any others you use (e.g. IBM Bob) here for Devpost.
