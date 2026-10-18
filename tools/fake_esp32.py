"""
Pretend to be the ESP32 black box — same messages, no hardware needed.

  python tools/fake_esp32.py                      # talks to http://localhost:8000
  python tools/fake_esp32.py https://xyz.lhr.life # or any server / tunnel link

Type a key + Enter while it runs:
  t  trip (sends last gasp)  / press again = homeowner reset
  w  toggle Wi-Fi lost (logs locally, uploads backlog when back)
  h  cell overheats -> at risk -> fails with OVERTEMP last gasp
  0-5  set discharge power in kW
  q  quit
Uses only the Python standard library.
"""
import json
import sys
import threading
import time
import urllib.request

SERVER = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000").rstrip("/")
ID, LAT, LON = "ESP32-01", 34.0205, -118.2856

s = {"state": "RUNNING", "wifi_lost": False, "kw": 3.0, "soc": 82.0, "temp": 27.0, "volt": 400.0}
pending = []
lock = threading.Lock()


def post(path, body):
    req = urllib.request.Request(SERVER + path, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status == 200
    except Exception as e:  # noqa: BLE001
        print("POST failed:", e)
        return False


def record(fault=None, kw=None):
    kw = s["kw"] if kw is None else kw
    mode = "DISCHARGE" if kw > 0.05 else "IDLE"
    return {"id": ID, "soc": round(s["soc"], 1), "power_kw": kw, "mode": mode,
            "temp_c": round(s["temp"], 1), "voltage": round(s["volt"], 1), "fault": fault,
            "lat": LAT, "lon": LON, "_t": time.time()}


def clean(r, age=False):
    out = {k: v for k, v in r.items() if k != "_t"}
    if age:
        out["age_s"] = round(time.time() - r["_t"], 1)
    return out


def last_gasp(fault):
    if not s["wifi_lost"]:
        post("/api/checkin", {**clean(record(fault, 0.0)), "last_gasp": True})
    print(f">> LAST GASP {fault}")


def loop():
    while True:
        time.sleep(1)
        with lock:
            st = s["state"]
            kw = s["kw"] if st in ("RUNNING", "OVERHEATING") else 0.0
            s["soc"] = max(0.0, s["soc"] - kw * (30 / 3600) / 13.5 * 100)
            if st == "OVERHEATING":
                s["temp"] += 4
            else:
                s["temp"] += (26 + 1.2 * kw - s["temp"]) * 0.3
            s["volt"] = 400 - 1.5 * kw - ((s["temp"] - 30) * 1.2 if st == "OVERHEATING" else 0)
            if st == "OVERHEATING" and s["temp"] >= 70:
                s["state"] = "FAILED"
                last_gasp("OVERTEMP")
                continue
            if st in ("TRIPPED", "FAILED"):
                continue
            r = record()
            if s["wifi_lost"]:
                pending.append(r)
                print(f"[offline] logged locally ({len(pending)} waiting)")
                continue
            if pending:
                if post("/api/backlog", {"id": ID, "records": [clean(p, age=True) for p in pending]}):
                    print(f">> backlog uploaded: {len(pending)} records")
                    pending.clear()
            ok = post("/api/checkin", clean(r))
            print(f"check-in {'ok' if ok else 'ERR'} kW={kw:.1f} SoC={s['soc']:.1f} T={s['temp']:.1f}")


threading.Thread(target=loop, daemon=True).start()
print(f"Fake ESP32 -> {SERVER}.  Keys: t trip/reset, w wifi, h overheat, 0-5 kW, q quit")
for line in sys.stdin:
    k = line.strip().lower()
    with lock:
        if k == "q":
            break
        if k == "t":
            if s["state"] == "RUNNING":
                s["state"] = "TRIPPED"; last_gasp("TRIP")
            else:
                s["state"], s["temp"] = "RUNNING", 30.0; print(">> reset by homeowner")
        elif k == "w":
            s["wifi_lost"] = not s["wifi_lost"]
            print(">> Wi-Fi LOST" if s["wifi_lost"] else ">> Wi-Fi BACK")
        elif k == "h" and s["state"] == "RUNNING":
            s["state"] = "OVERHEATING"; print(">> overheating")
        elif k in "012345" and k:
            s["kw"] = float(k); print(f">> power {k} kW")
