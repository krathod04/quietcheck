"""
Fleet simulator: ~500 home batteries, their smart meters, feeders and cellular carriers.

The simulator knows the *ground truth* (why a battery is silent). The diagnosis engine
never sees that truth -- it only sees the messages a real dispatcher would receive:
battery check-ins, last-gasp messages and smart-meter readings. The truth is exposed
separately so the dashboard can score the engine live ("god view").

Time is in ticks. 1 tick = one check-in interval (2 s real time, standing in for 30 s).
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .regions import DEFAULT_REGION, REGIONS

CARRIERS = REGIONS[DEFAULT_REGION]["carriers"]  # default region, kept for old imports

FEEDERS = REGIONS[DEFAULT_REGION]["feeders"]

TICK_HOURS = 30 / 3600  # each tick represents 30 seconds


@dataclass
class SimBattery:
    id: str
    lat: float
    lon: float
    feeder: str
    carrier: str
    medical: bool
    has_meter: bool = True
    capacity_kwh: float = 13.5
    max_kw: float = 5.0
    soc: float = 70.0
    setpoint_kw: float = 0.0  # + discharge, - charge
    home_load_kw: float = 1.5
    temp_c: float = 28.0
    voltage: float = 400.0
    # fault flags (ground truth)
    comms_down: bool = False
    tripped: bool = False
    hw_stage: int = 0  # 0 = healthy, 1..5 degrading, 6 = failed
    spoof: bool = False
    spoof_soc: float = 0.0
    send_last_gasp: bool = True
    gasped: bool = False
    pending_reply: tuple | None = None  # (tick_due, kind)

    @property
    def hw_failed(self) -> bool:
        return self.hw_stage >= 6


@dataclass
class Simulator:
    n: int = 500
    seed: int = 7
    tick: int = 0
    heatwave: bool = False
    feeders_out: set = field(default_factory=set)
    carriers_out: set = field(default_factory=set)
    blip_rate: float = 0.004  # chance a healthy check-in is randomly dropped
    replies: list = field(default_factory=list)
    region: str = DEFAULT_REGION

    def __post_init__(self):
        self.rng = random.Random(self.seed)
        reg = REGIONS[self.region]
        self.feeders, self.carriers = reg["feeders"], reg["carriers"]
        sy, sx = reg["spread"]
        self.batteries: dict[str, SimBattery] = {}
        for i in range(self.n):
            fname, flat, flon = self.feeders[i % len(self.feeders)]
            b = SimBattery(
                id=f"B{i:03d}",
                lat=flat + self.rng.gauss(0, sy),
                lon=flon + self.rng.gauss(0, sx),
                feeder=fname,
                carrier=self.rng.choices(self.carriers, weights=reg["carrier_weights"])[0],
                medical=self.rng.random() < 0.06,
                has_meter=self.rng.random() < reg["meter_coverage"],
                soc=self.rng.uniform(35, 98),
                home_load_kw=self.rng.uniform(0.6, 3.5),
            )
            b.setpoint_kw = self._new_setpoint()
            self.batteries[b.id] = b

    # ------------------------------------------------------------------ setup
    def _new_setpoint(self) -> float:
        r = self.rng.random()
        if r < 0.55:
            return round(self.rng.uniform(1.0, 5.0), 1)  # evening peak: discharging
        if r < 0.75:
            return round(-self.rng.uniform(1.0, 3.0), 1)  # charging
        return 0.0

    def static_info(self) -> list[dict]:
        return [
            {"id": b.id, "lat": round(b.lat, 5), "lon": round(b.lon, 5), "feeder": b.feeder,
             "carrier": b.carrier, "medical": b.medical, "max_kw": b.max_kw, "has_meter": b.has_meter}
            for b in self.batteries.values()
        ]

    # ------------------------------------------------------------------ faults
    def inject(self, kind: str, target: str | None = None) -> str:
        """Inject a fault. Returns a human-readable description."""
        rng = self.rng
        healthy = [b for b in self.batteries.values()
                   if not (b.comms_down or b.tripped or b.hw_stage or b.spoof)
                   and b.feeder not in self.feeders_out and b.carrier not in self.carriers_out]

        def pick() -> SimBattery:
            if target and target in self.batteries:
                return self.batteries[target]
            # prefer a discharging battery so the meter evidence is visible
            pool = [b for b in healthy if b.setpoint_kw > 1.5] or healthy
            return rng.choice(pool)

        if kind == "comms_loss":
            b = pick(); b.comms_down = True
            return f"{b.id}: home Wi-Fi/modem cut (battery still running)"
        if kind == "trip":
            b = pick(); b.tripped = True; b.send_last_gasp = True
            return f"{b.id}: battery tripped its breaker (sends last gasp)"
        if kind == "trip_silent":
            b = pick(); b.tripped = True; b.send_last_gasp = False
            return f"{b.id}: battery tripped with NO last gasp"
        if kind == "hw_fail":
            b = pick(); b.hw_stage = 1
            return f"{b.id}: cell failing -- overheating over the next few check-ins"
        if kind == "spoof":
            b = pick(); b.spoof = True; b.spoof_soc = round(b.soc, 1)
            return f"{b.id}: telemetry spoofed -- claims 5 kW discharge, actually offline"
        if kind == "feeder_outage":
            f = target if target in {f[0] for f in self.feeders} else rng.choice(
                [f[0] for f in self.feeders if f[0] not in self.feeders_out])
            self.feeders_out.add(f)
            return f"Grid outage on feeder {f}"
        if kind == "carrier_outage":
            c = target if target in self.carriers else rng.choice(
                [c for c in self.carriers if c not in self.carriers_out] or self.carriers)
            self.carriers_out.add(c)
            return f"Cellular outage: {c} network down"
        if kind == "heatwave":
            self.heatwave = not self.heatwave
            return f"Heatwave {'ON (ambient 42 C)' if self.heatwave else 'OFF'}"
        if kind == "restore":
            self.feeders_out.clear(); self.carriers_out.clear()
            for b in self.batteries.values():
                b.comms_down = False
            return "Grid feeders and networks restored"
        raise ValueError(f"unknown fault: {kind}")

    def homeowner_contact(self, battery_id: str, kind: str) -> None:
        """Engine asked the homeowner to do something; schedule a realistic reply."""
        b = self.batteries.get(battery_id)
        if not b or b.pending_reply:
            return
        b.pending_reply = (self.tick + self.rng.randint(2, 4), kind)

    # ------------------------------------------------------------------ truth
    def truth(self, b: SimBattery) -> str:
        if b.feeder in self.feeders_out:
            return "GRID_OUTAGE"
        if b.hw_failed:
            return "HARDWARE_FAILING"
        if b.tripped:
            return "TRIPPED"
        if b.carrier in self.carriers_out:
            return "NETWORK_OUTAGE"
        if b.comms_down:
            return "COMMS_LOSS"
        if b.spoof:
            return "TELEMETRY_MISMATCH"
        if b.hw_stage:
            return "AT_RISK"
        return "OK"

    def truth_capacity_kw(self) -> float:
        """kW the fleet could *actually* deliver to the grid right now."""
        total = 0.0
        for b in self.batteries.values():
            if b.feeder in self.feeders_out or b.tripped or b.hw_failed or b.spoof:
                continue
            if b.soc > 20:
                total += b.max_kw
        return round(total, 1)

    # ------------------------------------------------------------------ step
    def step(self) -> dict:
        """Advance one tick. Returns the messages the outside world would see."""
        self.tick += 1
        t = self.tick
        rng = self.rng
        checkins, gasps, meters = [], [], []
        self.replies = []
        ambient = 42.0 if self.heatwave else 26.0

        for b in self.batteries.values():
            # homeowner replies to an earlier text/call
            if b.pending_reply and t >= b.pending_reply[0]:
                self._resolve_reply(b)

            # wander the household load and occasionally change dispatch setpoint
            b.home_load_kw = min(6.0, max(0.3, b.home_load_kw + rng.gauss(0, 0.15)))
            if rng.random() < 0.01:
                b.setpoint_kw = self._new_setpoint()

            grid_down = b.feeder in self.feeders_out

            # hardware degradation progresses one stage per tick
            if 0 < b.hw_stage < 6:
                b.hw_stage += 1

            # actual power delivered
            if b.tripped or b.hw_failed or b.spoof:
                actual_kw = 0.0
            elif grid_down:
                actual_kw = min(b.home_load_kw, b.max_kw) if b.soc > 5 else 0.0  # islanding
            else:
                actual_kw = b.setpoint_kw
                if actual_kw > 0 and b.soc <= 10:
                    actual_kw = 0.0
                if actual_kw < 0 and b.soc >= 99:
                    actual_kw = 0.0
            b.soc = min(100.0, max(0.0, b.soc - actual_kw * TICK_HOURS / b.capacity_kwh * 100))

            # thermals / voltage (the black-box signals)
            target_temp = ambient + 1.2 * abs(actual_kw) + (6.0 * b.hw_stage if b.hw_stage else 0)
            b.temp_c += (target_temp - b.temp_c) * 0.5 + rng.gauss(0, 0.3)
            b.voltage = 400 - 7.0 * b.hw_stage - 1.5 * actual_kw + rng.gauss(0, 0.8)

            # last gasp on the tick the battery trips
            if b.tripped and b.send_last_gasp and not b.gasped:
                b.gasped = True
                if not (b.comms_down or grid_down or b.carrier in self.carriers_out):
                    gasps.append(self._record(b, 0.0, fault="TRIP", last_gasp=True))

            # regular check-in
            silent = (b.comms_down or b.tripped or b.hw_failed or grid_down
                      or b.carrier in self.carriers_out)
            if not silent and rng.random() > self.blip_rate:
                if b.spoof:
                    rec = self._record(b, 5.0)
                    rec["soc"] = b.spoof_soc
                    rec["mode"] = "DISCHARGE"
                    rec["temp_c"] = round(ambient + 4, 1)
                else:
                    rec = self._record(b, actual_kw)
                checkins.append(rec)

            # smart meter (separate AMI network, but needs grid power)
            if not grid_down and b.has_meter:
                meters.append({"id": b.id, "tick": t,
                               "import_kw": round(b.home_load_kw - actual_kw, 2)})

        return {"tick": t, "checkins": checkins, "gasps": gasps, "meters": meters,
                "replies": self.replies}

    def _record(self, b: SimBattery, kw: float, fault: str | None = None, last_gasp=False) -> dict:
        mode = "DISCHARGE" if kw > 0.05 else "CHARGE" if kw < -0.05 else "IDLE"
        return {"id": b.id, "tick": self.tick, "soc": round(b.soc, 1), "power_kw": round(kw, 2),
                "mode": mode, "temp_c": round(b.temp_c, 1), "voltage": round(b.voltage, 1),
                "fault": fault, "last_gasp": last_gasp}

    def _resolve_reply(self, b: SimBattery) -> None:
        _, kind = b.pending_reply
        b.pending_reply = None
        if b.tripped:
            b.tripped = False
            b.gasped = False
            msg = "Pressed the reset button, light is green again"
        elif b.hw_failed:
            msg = "Light is RED and the unit is hot to the touch"
        elif b.comms_down:
            b.comms_down = False
            msg = "Restarted the router"
        elif b.feeder in self.feeders_out:
            msg = "Whole street is dark, battery is keeping our lights on"
        elif b.carrier in self.carriers_out:
            msg = "Battery screen looks normal"
        else:
            msg = "Everything looks normal here"
        self.replies.append({"id": b.id, "tick": self.tick, "kind": kind, "text": msg})
