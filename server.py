"""
QuietCheck backend.  Run:  python server.py   then open http://localhost:8000

* Runs the 500-battery simulator in the background (1 tick every TICK_SECONDS).
* Real devices (e.g. the ESP32 black box) POST to /api/checkin like any simulated battery.
* The dashboard polls /api/state once a second.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from quietcheck.fleet import Fleet
from quietcheck.notify import Notifier

TICK_SECONDS = float(os.getenv("TICK_SECONDS", "2"))
N_BATTERIES = int(os.getenv("N_BATTERIES", "500"))
STATIC = Path(__file__).parent / "static"

fleet = Fleet(n=N_BATTERIES, region=os.getenv("REGION", "la"))
running = {"on": True}
notifier = Notifier()   # real-phone alerts for real devices (set NTFY_TOPIC)


async def ticker():
    while True:
        await asyncio.sleep(TICK_SECONDS)
        if running["on"]:
            fleet.step()
            notifier.check(fleet.engine)
            notifier.alarms(fleet.alarms)


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(ticker())
    yield
    task.cancel()


app = FastAPI(title="QuietCheck", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


# ---------------------------------------------------------------- dashboard
@app.get("/api/init")
async def init():
    eng = fleet.engine
    return {"batteries": [{"id": b.id, "lat": b.lat, "lon": b.lon, "feeder": b.feeder,
                           "carrier": b.carrier, "medical": b.medical}
                          for b in eng.batteries.values()],
            "options": fleet.options(), "tick_seconds": TICK_SECONDS,
            "gateways": fleet.gateway_points()}


@app.post("/api/region")
async def region(payload: dict):
    """Switch the whole fleet to another city preset (Twist 2): {"region": "mumbai"}"""
    from quietcheck.regions import REGIONS
    key = payload.get("region")
    if key not in REGIONS:
        raise HTTPException(400, f"unknown region; choose one of {sorted(REGIONS)}")
    fleet.reset(region=key)
    return {"ok": True, "region": key}


@app.get("/api/state")
async def state(since: int = 0):
    eng = fleet.engine
    externals = [{"id": b.id, "lat": b.lat, "lon": b.lon, "feeder": b.feeder,
                  "carrier": b.carrier, "medical": b.medical}
                 for b in eng.batteries.values() if b.external]
    return {"metrics": fleet.metrics(), "statuses": fleet.statuses(),
            "feed": [e for e in list(eng.feed)[:80] if e["seq"] > since],
            "history": fleet.history[-90:], "externals": externals, "running": running["on"],
            "alarms": [a for a in fleet.alarms if not a["acked"]],
            "tickets": [_brief(t) for t in fleet.tickets.list(limit=40)]}


def _brief(t):
    return {k: t[k] for k in ("id", "kind", "title", "scope", "key", "severity", "status", "opened_tick",
                              "closed_tick", "naive_visits", "our_visits")} | {"n": len(t["batteries"])}


# ---------------------------------------------------------------- tickets (investigation reports)
@app.get("/api/tickets")
async def tickets(battery: str | None = None, status: str | None = None, limit: int = 200):
    """All tickets, or one house's history with ?battery=B012. Newest open first."""
    return {"summary": fleet.tickets.summary(), "tickets": fleet.tickets.list(battery, status, limit)}


@app.get("/api/tickets.csv")
async def tickets_csv():
    """One row per ticket per battery (house), for analysis in a spreadsheet."""
    return PlainTextResponse(fleet.tickets.csv(fleet.sim), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=quietcheck_tickets.csv"})


@app.get("/api/tickets/{ticket_id}")
async def ticket(ticket_id: str):
    t = fleet.tickets.items.get(ticket_id)
    if t is None:
        raise HTTPException(404, "unknown ticket")
    return t


@app.post("/api/push/claim")
async def push_claim(payload: dict | None = None):
    """Alerts the server couldn't deliver (ntfy rate limit); the dashboard relays them."""
    from quietcheck.notify import claim_relay
    return claim_relay()


@app.post("/api/alarms/ack")
async def ack(payload: dict):
    fleet.ack_alarm(int(payload.get("id", 0)))
    return {"ok": True}


@app.get("/api/battery/{battery_id}")
async def battery(battery_id: str):
    d = fleet.engine.detail(battery_id)
    if d is None:
        raise HTTPException(404, "unknown battery")
    sb = fleet.sim.batteries.get(battery_id)
    d["truth"] = fleet.sim.truth(sb) if sb else None
    d["tickets"] = [_brief(t) for t in fleet.tickets.list(battery=battery_id)]
    return d


@app.post("/api/inject")
async def inject(payload: dict):
    try:
        desc = fleet.inject(payload.get("kind", ""), payload.get("target") or None)
    except (ValueError, IndexError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "description": desc}


@app.post("/api/reset")
async def reset(payload: dict | None = None):
    fleet.reset()
    return {"ok": True}


@app.post("/api/pause")
async def pause(payload: dict | None = None):
    running["on"] = not running["on"]
    return {"running": running["on"]}


@app.post("/api/step")
async def step(payload: dict | None = None):
    fleet.step()
    return {"tick": fleet.sim.tick}


# ---------------------------------------------------------------- real devices
@app.post("/api/checkin")
async def checkin(payload: dict):
    """One check-in from a real device. Required: id. Typical:
    {"id":"ESP32-01","soc":81.5,"power_kw":2.1,"mode":"DISCHARGE","temp_c":31.2,
     "voltage":398.4,"fault":null,"lat":34.0205,"lon":-118.2856}"""
    if "id" not in payload:
        raise HTTPException(400, "id required")
    for k, v in {"soc": 0, "power_kw": 0, "mode": "IDLE", "temp_c": 25, "voltage": 400}.items():
        payload.setdefault(k, v)
    eng = fleet.engine
    if payload["id"] not in eng.batteries:
        eng.register({**payload, "feeder": payload.get("feeder", "EXT"),
                      "carrier": payload.get("carrier", "WiFi")}, external=True)
    if payload.get("last_gasp"):
        eng.ingest_last_gasp(payload, fleet.sim.tick)
    else:
        eng.ingest_checkin(payload, fleet.sim.tick)
    return {"ok": True, "tick": fleet.sim.tick}


@app.post("/api/backlog")
async def backlog(payload: dict):
    """Store-and-forward upload after reconnecting: {"id": "...", "records": [ {...}, ... ]}
    Records may carry "age_s" (seconds before upload) instead of a server tick."""
    records = payload.get("records", [])
    now = fleet.sim.tick
    for r in records:
        if "tick" not in r:
            r["tick"] = max(0, now - int(round(float(r.get("age_s", 0)) / TICK_SECONDS)))
    fleet.engine.ingest_backlog(payload["id"], records, now)
    return {"ok": True, "count": len(records)}


@app.post("/api/meter")
async def meter(payload: dict):
    """Optional smart-meter reading for a real device: {"id": "...", "import_kw": 1.2}"""
    fleet.engine.ingest_meter(payload, fleet.sim.tick)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
