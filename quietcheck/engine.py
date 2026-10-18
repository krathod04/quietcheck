"""
Diagnosis engine: turns silence into a decision.

Inputs are only what a real fleet dispatcher receives:
  * battery check-ins (the battery's own telemetry, every interval)
  * last-gasp messages (one final packet sent as a battery goes down)
  * smart-meter readings (a separate network that measures the home's grid import)
  * store-and-forward backlogs (records a battery logged while it was offline)

For every battery that goes quiet, the engine cross-checks the other signals that are
still talking and picks a cause, a confidence, the reasons, and the gentlest response
on the ladder: WATCH -> PING -> ASK -> HOLD -> DISPATCH (hand to a person).

It also watches batteries that ARE talking, for two pre-emptive cases:
  * AT_RISK            -- black-box trend shows overheating / voltage sag before failure
  * TELEMETRY_MISMATCH -- the battery is talking, but its numbers break physics
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

# Debounce, like CAN error counters: one missed message never raises an alarm.
WATCH_AFTER = 2      # consecutive missed check-ins before we start watching
DIAGNOSE_AFTER = 3   # consecutive missed check-ins before we diagnose
ASK_TIMEOUT = 8      # ticks to wait for a homeowner before dispatching (tripped)
PING_TIMEOUT = 6     # ticks of fallback pings before asking about the router
BLACKBOX_LEN = 40    # records kept per battery

LADDER = ["WATCH", "PING", "ASK", "HOLD", "DISPATCH"]

LABELS = {
    "OK":                 {"title": "Healthy",              "silent": False},
    "WATCH":              {"title": "Missed check-ins",     "silent": True},
    "COMMS_LOSS":         {"title": "Comms loss",           "silent": True},
    "NETWORK_OUTAGE":     {"title": "Carrier outage",       "silent": True},
    "GRID_OUTAGE":        {"title": "Grid outage",          "silent": True},
    "GATEWAY_FAULT":      {"title": "Gateway fault",        "silent": True},
    "TRIPPED":            {"title": "Battery tripped",      "silent": True},
    "HARDWARE_FAILING":   {"title": "Hardware failure",     "silent": True},
    "UNKNOWN":            {"title": "Unknown",              "silent": True},
    "AT_RISK":            {"title": "At risk (pre-emptive)", "silent": False},
    "TELEMETRY_MISMATCH": {"title": "Talking but wrong",    "silent": False},
}


@dataclass
class BatteryState:
    id: str
    lat: float
    lon: float
    feeder: str
    carrier: str
    medical: bool = False
    gateway: str | None = None      # neighbourhood comms gateway shared with nearby homes
    max_kw: float = 5.0
    external: bool = False          # a real device (ESP32) rather than simulated
    last_seen: int = -1
    blackbox: deque = field(default_factory=lambda: deque(maxlen=BLACKBOX_LEN))
    last_gasp: dict | None = None
    label: str = "OK"
    confidence: float = 1.0
    reasons: list = field(default_factory=list)
    ladder: str | None = None
    ladder_since: int = 0
    diag_since: int = 0
    asked: bool = False
    dispatched: bool = False
    contact_log: list = field(default_factory=list)

    @property
    def last(self) -> dict | None:
        return self.blackbox[-1] if self.blackbox else None


@dataclass
class MeterState:
    last_seen: int = -1
    history: deque = field(default_factory=lambda: deque(maxlen=BLACKBOX_LEN))


class Engine:
    def __init__(self):
        self.batteries: dict[str, BatteryState] = {}
        self.meters: dict[str, MeterState] = {}
        self.tick = 0
        self.heatwave = False
        self.feed: deque = deque(maxlen=200)
        self.contact = {"channel": "SMS", "language": "English"}  # set per region by Fleet
        self.outbox: list[dict] = []      # homeowner contacts for the outside world
        self.stats = {"blips_ignored": 0, "trucks_avoided": 0, "trucks_sent": 0,
                      "resolved_remotely": 0}
        self._seq = 0
        self.gateway_dispatched: dict[str, int] = {}   # gateway -> tick one engineer was sent
        self.work_orders: list[dict] = []              # engineer visits for the outside world

    # ================================================================ ingest
    def register(self, info: dict, external: bool = False) -> BatteryState:
        b = self.batteries.get(info["id"])
        if b is None:
            b = BatteryState(id=info["id"], lat=info.get("lat", 34.0205),
                             lon=info.get("lon", -118.2856),
                             feeder=info.get("feeder", "EXT"), carrier=info.get("carrier", "WiFi"),
                             medical=bool(info.get("medical", False)),
                             max_kw=float(info.get("max_kw", 5.0)), external=external,
                             gateway=info.get("gateway") or None)
            self.batteries[b.id] = b
            self.meters.setdefault(b.id, MeterState())
        return b

    def ingest_checkin(self, rec: dict, tick: int | None = None) -> None:
        tick = self.tick if tick is None else tick
        b = self.batteries.get(rec["id"]) or self.register(rec, external=True)
        rec = dict(rec, tick=rec.get("tick", tick))
        gap = tick - b.last_seen if b.last_seen >= 0 else 0
        b.blackbox.append(rec)
        b.last_seen = tick
        if rec.get("last_gasp"):
            b.last_gasp = rec
        if gap == 2 and b.label == "OK":
            self.stats["blips_ignored"] += 1   # one dropped packet, debounced
        if LABELS.get(b.label, {}).get("silent"):
            self._on_return(b, tick)

    def ingest_last_gasp(self, rec: dict, tick: int | None = None) -> None:
        tick = self.tick if tick is None else tick
        b = self.batteries.get(rec["id"]) or self.register(rec, external=True)
        rec = dict(rec, tick=rec.get("tick", tick), last_gasp=True)
        b.blackbox.append(rec)
        b.last_gasp = rec
        # a last gasp is the final word: do not count it as "alive"
        self._event(b, "LAST_GASP", f"Last gasp received: fault {rec.get('fault') or 'n/a'}",
                    kind="signal")

    def ingest_meter(self, rec: dict, tick: int | None = None) -> None:
        tick = self.tick if tick is None else tick
        m = self.meters.setdefault(rec["id"], MeterState())
        m.last_seen = tick
        m.history.append((tick, float(rec["import_kw"])))

    def ingest_backlog(self, battery_id: str, records: list[dict], tick: int | None = None) -> None:
        """Store-and-forward upload after a reconnect: proves what happened while silent."""
        tick = self.tick if tick is None else tick
        b = self.batteries.get(battery_id)
        if b is None or not records:
            return
        for r in records:
            b.blackbox.append(dict(r, id=battery_id, backlog=True))
        running = sum(1 for r in records if r.get("mode") in ("DISCHARGE", "CHARGE", "IDLE")
                      and not r.get("fault"))
        verdict = ("ran normally the whole time -- it was only a comms loss"
                   if running == len(records) else
                   f"logged {len(records) - running} fault record(s) while offline")
        self._event(b, "BACKLOG", f"Store-and-forward: {len(records)} records uploaded; {verdict}",
                    kind="resolved")

    # ================================================================ evaluate
    def evaluate(self, tick: int, heatwave: bool = False) -> None:
        self.tick = tick
        self.heatwave = heatwave
        feeder_stats = self._feeder_stats(tick)
        carrier_stats = self._carrier_stats(tick)
        self._gw_stats = self._gateway_stats(tick)

        for b in self.batteries.values():
            missed = tick - b.last_seen if b.last_seen >= 0 else 0
            if missed >= DIAGNOSE_AFTER:
                label, conf, reasons = self._diagnose_silent(b, tick, feeder_stats, carrier_stats,
                                                             self._gw_stats)
                self._set(b, label, conf, reasons, tick)
                self._advance_ladder(b, tick)
            elif missed >= WATCH_AFTER:
                if b.label in ("OK", "AT_RISK", "TELEMETRY_MISMATCH"):
                    self._set(b, "WATCH", 0.5,
                              [f"{missed - 1} check-in(s) missed -- debouncing, no action yet"], tick)
            else:
                label, conf, reasons = self._diagnose_live(b)
                self._set(b, label, conf, reasons, tick)
                if label == "OK":
                    b.ladder = None
        # a repaired gateway can be dispatched to again next time
        live = {b.gateway for b in self.batteries.values() if b.label == "GATEWAY_FAULT"}
        for g in list(self.gateway_dispatched):
            if g not in live:
                del self.gateway_dispatched[g]

    # ---------------------------------------------------------------- silent
    def _diagnose_silent(self, b, tick, feeder_stats, carrier_stats, gw_stats=None):
        reasons = []
        m = self.meters.get(b.id)
        meter_alive = m is not None and m.last_seen >= 0 and tick - m.last_seen < DIAGNOSE_AFTER
        has_meter = m is not None and m.last_seen >= 0
        last = self._last_live(b)
        missed = tick - b.last_seen
        reasons.append(f"Silent for {missed - 1} check-ins")

        # 1. Whole feeder dark -> grid outage (meters need grid power to report)
        fs = feeder_stats.get(b.feeder)
        if has_meter and not meter_alive and fs and fs["meters"] >= 5 and fs["meter_silent_frac"] >= 0.5:
            reasons.append(f"{fs['meter_silent']}/{fs['meters']} smart meters on {b.feeder} are also dark")
            reasons.append("Battery is most likely islanding the home -- not a battery fault")
            return "GRID_OUTAGE", 0.93, reasons

        # 2. Last gasp with a fault code is the strongest single piece of evidence
        if b.last_gasp and b.last_gasp.get("tick", -1) >= b.last_seen - 1:
            fault = b.last_gasp.get("fault") or "UNKNOWN"
            reasons.append(f"Last gasp received with fault code {fault}")
            if fault in ("OVERTEMP", "CELL_FAULT", "BMS_FAULT"):
                return "HARDWARE_FAILING", 0.95, reasons
            if self.heatwave:
                reasons.append("Heatwave in effect -- thermal trips are more likely")
            return "TRIPPED", 0.96 if fault == "TRIP" else 0.8, reasons

        # 3. Black box trend before silence: overheating or voltage sag
        trend = self._trend(b)
        if trend:
            reasons.extend(trend)
            if meter_alive and self._meter_jump(b, last):
                reasons.append("Smart meter shows the home now pulling from the grid")
                return "HARDWARE_FAILING", 0.95, reasons
            return "HARDWARE_FAILING", 0.85, reasons

        # 4. Many batteries on the same carrier silent while their meters are fine
        cs = carrier_stats.get(b.carrier)
        if meter_alive and cs and cs["silent"] >= 5 and cs["silent_frac"] >= 0.25:
            reasons.append(f"{cs['silent']}/{cs['total']} batteries on {b.carrier} went quiet together; "
                           "their meters still report")
            reasons.append(f"Pattern matches a carrier outage, not {cs['silent']} separate battery faults")
            return "NETWORK_OUTAGE", 0.9, reasons

        # 4b. Several neighbours behind the same gateway silent at once, meters fine, carrier fine:
        #     the shared gateway failed, not each battery. One engineer, not one visit per home.
        gs = (gw_stats or {}).get(b.gateway) if b.gateway else None
        if meter_alive and gs and gs["silent"] >= 3 and gs["silent_frac"] >= 0.6:
            reasons.append(f"{gs['silent']}/{gs['total']} batteries behind gateway {b.gateway} went quiet "
                           f"within {gs['spread'] + 1} check-in(s) of each other")
            reasons.append("Their smart meters still show each home's battery covering its load")
            if cs:
                others = cs["total"] - cs["silent"]
                reasons.append(f"{others} other {b.carrier} batteries are still reporting: not a carrier outage")
            reasons.append(f"Common point of failure: gateway {b.gateway}. One engineer to the gateway "
                           f"instead of {gs['silent']} home visits")
            return "GATEWAY_FAULT", 0.92, reasons

        # 5. Smart meter evidence for this one home
        if meter_alive:
            if self._meter_jump(b, last):
                reasons.append(f"Smart meter import jumped by ~{self._jump_kw(b, last):.1f} kW: "
                               "the battery stopped covering the home")
                reasons.append("No last gasp and no overheating trend before it went quiet")
                if self.heatwave:
                    reasons.append("Heatwave in effect -- thermal trips are more likely")
                return "TRIPPED", 0.78 if not self.heatwave else 0.83, reasons
            was_active = last is not None and abs(last.get("power_kw", 0)) > 0.5
            reasons.append("Smart meter still reports normally, home load unchanged")
            if was_active:
                reasons.append(f"Battery was {last['mode'].lower()} at {abs(last['power_kw']):.1f} kW and the "
                               "meter shows it still is")
                return "COMMS_LOSS", 0.88, reasons
            reasons.append("Battery was idle, so the meter can't fully confirm it's still running")
            return "COMMS_LOSS", 0.65, reasons

        if not has_meter:
            # Low smart-meter coverage (e.g. Mumbai): borrow the evidence from neighbours instead
            if fs and fs["meters"] >= 3 and fs["meter_silent_frac"] >= 0.5:
                reasons.append(f"No meter of its own, but {fs['meter_silent']}/{fs['meters']} meters on "
                               f"{b.feeder} are dark")
                return "GRID_OUTAGE", 0.8, reasons
            if cs and cs["silent"] >= 5 and cs["silent_frac"] >= 0.25:
                reasons.append(f"No meter of its own, but {cs['silent']}/{cs['total']} metered batteries on "
                               f"{b.carrier} went quiet together")
                return "NETWORK_OUTAGE", 0.78, reasons
            if gs and gs["silent"] >= 3 and gs["silent_frac"] >= 0.5:
                reasons.append(f"No meter of its own, but {gs['silent']}/{gs['total']} metered neighbours behind "
                               f"gateway {b.gateway} went quiet at the same time")
                return "GATEWAY_FAULT", 0.75, reasons
            reasons.append("No smart meter paired with this unit and its neighbours look fine")
        else:
            reasons.append("This home's meter is also silent, but the feeder is mostly fine")
        return "UNKNOWN", 0.4, reasons

    # ---------------------------------------------------------------- live
    def _diagnose_live(self, b):
        recs = [r for r in b.blackbox if not r.get("last_gasp")][-6:]
        if len(recs) >= 5:
            # physics check: claims discharge but state of charge doesn't move
            discharging = [r for r in recs if r.get("power_kw", 0) > 1.0]
            if len(discharging) >= 5:
                expected_drop = sum(r["power_kw"] for r in recs[1:]) * (30 / 3600) / 13.5 * 100
                actual_drop = recs[0]["soc"] - recs[-1]["soc"]
                if expected_drop > 0.8 and actual_drop < 0.25 * expected_drop:
                    reasons = [f"Claims {recs[-1]['power_kw']:.1f} kW discharge for {len(recs)} check-ins",
                               f"Charge should have fallen ~{expected_drop:.1f}% but moved {actual_drop:.1f}%"]
                    m = self.meters.get(b.id)
                    if m and len(m.history) >= 2:
                        imp = m.history[-1][1]
                        if imp > 0.5:
                            reasons.append(f"Smart meter says the home is importing {imp:.1f} kW -- "
                                           "battery is not actually discharging")
                    reasons.append("Numbers break physics: possible spoofing or a stuck sensor")
                    return "TELEMETRY_MISMATCH", 0.9, reasons
            trend = self._trend(b, window=5)
            if trend:
                return "AT_RISK", 0.8, trend + ["Still talking -- act before it fails"]
        return "OK", 1.0, []

    def _fleet_temp(self, window: int) -> tuple[float, float]:
        """Median temperature rise and median current temperature across talking batteries.
        Weather (a heatwave) warms the whole fleet at once; a failing cell warms alone."""
        key = (self.tick, window)
        if getattr(self, "_fleet_cache_key", None) == key:
            return self._fleet_cache
        rises, temps = [], []
        for o in self.batteries.values():
            if self.tick - o.last_seen > 1:
                continue
            rr = [r for r in o.blackbox if not r.get("last_gasp") and not r.get("backlog")][-window:]
            if len(rr) >= 3:
                rises.append(rr[-1]["temp_c"] - rr[0]["temp_c"])
                temps.append(rr[-1]["temp_c"])
        med = lambda xs: sorted(xs)[len(xs) // 2] if xs else 0.0
        self._fleet_cache_key, self._fleet_cache = key, (med(rises), med(temps) if temps else 26.0)
        return self._fleet_cache

    def _trend(self, b, window: int = 6) -> list[str]:
        recs = [r for r in b.blackbox if not r.get("last_gasp") and not r.get("backlog")][-window:]
        if len(recs) < 3:
            return []
        out = []
        dt = recs[-1]["temp_c"] - recs[0]["temp_c"]
        fleet_rise, fleet_temp = self._fleet_temp(window)
        excess_rise = dt - max(0.0, fleet_rise)
        excess_temp = recs[-1]["temp_c"] - fleet_temp
        if excess_temp >= 20 or excess_rise >= 12:
            note = (f" while the fleet rose {fleet_rise:.0f} C" if fleet_rise >= 3 else "")
            out.append(f"Black box: temperature climbed {recs[0]['temp_c']:.0f} -> {recs[-1]['temp_c']:.0f} C "
                       f"over the last {len(recs)} check-ins{note}")
        dv = recs[0]["voltage"] - recs[-1]["voltage"]
        if dv >= 15:
            out.append(f"Black box: pack voltage sagged {dv:.0f} V before it went quiet")
        return out

    # ---------------------------------------------------------------- helpers
    def _last_live(self, b):
        for r in reversed(b.blackbox):
            if not r.get("last_gasp"):
                return r
        return None

    def _meter_baseline(self, b, last):
        m = self.meters.get(b.id)
        if not m or not last:
            return None, None
        before = [v for t, v in m.history if t <= last["tick"]][-3:]
        after = [v for t, v in m.history if t > last["tick"] + 1][-3:]
        if not before or not after:
            return None, None
        return sum(before) / len(before), sum(after) / len(after)

    def _jump_kw(self, b, last):
        a, c = self._meter_baseline(b, last)
        return 0.0 if a is None else c - a

    def _meter_jump(self, b, last) -> bool:
        if not last or last.get("power_kw", 0) <= 0.8:
            return False
        jump = self._jump_kw(b, last)
        return jump >= 0.6 * last["power_kw"]

    def _feeder_stats(self, tick):
        stats = {}
        for b in self.batteries.values():
            m = self.meters.get(b.id)
            if not m or m.last_seen < 0:
                continue
            s = stats.setdefault(b.feeder, {"meters": 0, "meter_silent": 0})
            s["meters"] += 1
            if tick - m.last_seen >= DIAGNOSE_AFTER:
                s["meter_silent"] += 1
        for s in stats.values():
            s["meter_silent_frac"] = s["meter_silent"] / s["meters"] if s["meters"] else 0
        return stats

    def _gateway_stats(self, tick):
        """Per gateway: how many of its batteries went silent (no last gasp) while their meters
        still report, and how close together they stopped."""
        stats = {}
        for b in self.batteries.values():
            if not b.gateway or b.last_seen < 0:
                continue
            s = stats.setdefault(b.gateway, {"total": 0, "silent": 0, "stops": [], "ids": []})
            s["total"] += 1
            m = self.meters.get(b.id)
            meter_alive = m and m.last_seen >= 0 and tick - m.last_seen < DIAGNOSE_AFTER
            gasped = b.last_gasp and b.last_gasp.get("tick", -1) >= b.last_seen - 1
            if tick - b.last_seen >= DIAGNOSE_AFTER and meter_alive and not gasped:
                s["silent"] += 1
                s["stops"].append(b.last_seen)
                s["ids"].append(b.id)
        for s in stats.values():
            s["silent_frac"] = s["silent"] / s["total"] if s["total"] else 0
            s["spread"] = (max(s["stops"]) - min(s["stops"])) if s["stops"] else 0
        return stats

    def _carrier_stats(self, tick):
        stats = {}
        for b in self.batteries.values():
            if b.last_seen < 0:
                continue
            m = self.meters.get(b.id)
            meter_alive = m and m.last_seen >= 0 and tick - m.last_seen < DIAGNOSE_AFTER
            if not meter_alive:
                continue  # grid outages shouldn't count as carrier evidence
            s = stats.setdefault(b.carrier, {"total": 0, "silent": 0})
            s["total"] += 1
            if tick - b.last_seen >= DIAGNOSE_AFTER and not (
                    b.last_gasp and b.last_gasp.get("tick", -1) >= b.last_seen - 1):
                s["silent"] += 1
        for s in stats.values():
            s["silent_frac"] = s["silent"] / s["total"] if s["total"] else 0
        return stats

    # ================================================================ ladder
    def _set(self, b, label, conf, reasons, tick):
        changed = label != b.label
        b.confidence, b.reasons = conf, reasons
        if not changed:
            return
        prev = b.label
        b.label = label
        b.diag_since = tick
        if label in ("OK", "WATCH"):
            if label == "OK" and prev in ("AT_RISK", "TELEMETRY_MISMATCH"):
                self._event(b, "OK", "Readings back to normal", kind="resolved")
            return
        action = {
            "COMMS_LOSS": "Ping over fallback path (meter network). Keep its capacity. No truck.",
            "NETWORK_OUTAGE": "Hold and wait it out. Flag the carrier. Keep its capacity. No truck.",
            "GRID_OUTAGE": "Hand to the utility. Battery is backing up the home. No truck.",
            "GATEWAY_FAULT": (f"Shared gateway {b.gateway} is down. Batteries keep running; hold their capacity. "
                              "One engineer to the gateway, no home visits."),
            "TRIPPED": ("Call the homeowner with reset steps (medical-device home, priority)."
                        if b.medical else "Text the homeowner reset steps. Hold its capacity."),
            "HARDWARE_FAILING": "Remove from dispatch. Send a technician with replacement parts.",
            "UNKNOWN": "Ask the homeowner what they see before doing anything else.",
            "AT_RISK": "Pre-emptive: cap discharge at 50% and book an inspection on a normal day.",
            "TELEMETRY_MISMATCH": "Don't trust its numbers. Remove from capacity. Flag for security review.",
        }[label]
        self._event(b, label, action, kind="decision")

    def _advance_ladder(self, b, tick):
        L = b.label
        age = tick - b.diag_since
        if L == "COMMS_LOSS":
            if b.ladder is None:
                b.ladder, b.ladder_since = "PING", tick
            elif b.ladder == "PING" and age >= PING_TIMEOUT and not b.asked:
                self._ask(b, tick, "router")
        elif L == "NETWORK_OUTAGE":
            b.ladder = b.ladder if b.ladder == "HOLD" else "HOLD"
        elif L == "GATEWAY_FAULT":
            b.ladder = "HOLD"
            g = b.gateway
            if g and g not in self.gateway_dispatched:
                self.gateway_dispatched[g] = tick
                self.stats["trucks_sent"] += 1
                n = (self._gw_stats.get(g) or {}).get("silent", 0)
                self.stats["trucks_avoided"] += max(0, n - 1)
                self.work_orders.append({"gateway": g, "tick": tick, "homes": n})
                self._event(b, "DISPATCH", f"One engineer dispatched to gateway {g} (covers {n} silent homes). "
                            f"{max(0, n - 1)} home visits avoided.", kind="dispatch")
        elif L == "GRID_OUTAGE":
            b.ladder = "DISPATCH" if b.ladder == "DISPATCH" else "HOLD"
        elif L in ("TRIPPED", "UNKNOWN"):
            if not b.asked:
                self._ask(b, tick, "reset" if L == "TRIPPED" else "check")
            elif (L == "TRIPPED" and b.ladder == "ASK" and tick - b.ladder_since >= ASK_TIMEOUT
                  and not b.dispatched):
                self._dispatch(b, tick, "No reply from homeowner")
        elif L == "HARDWARE_FAILING":
            if not b.dispatched:
                b.ladder = "HOLD"
                self._dispatch(b, tick, "Hardware failure confirmed")

    def _ask(self, b, tick, kind):
        b.asked = True
        b.ladder, b.ladder_since = "ASK", tick
        channel = "voice call" if b.medical else self.contact["channel"]
        text = {
            "reset": "Your home battery stopped responding. Is its light red? Press RESET for 3 s. Reply 1 = done, 2 = still red.",
            "router": "We can't reach your battery. Could you restart your internet router? Reply 1 when done.",
            "check": "We lost contact with your battery. What color is its status light? Reply G / R / OFF.",
        }[kind]
        b.contact_log.append({"tick": tick, "dir": "out", "channel": channel, "text": text})
        self.outbox.append({"id": b.id, "kind": kind, "channel": channel})
        lang = self.contact["language"]
        sent = f" (sent in {lang})" if lang != "English" and not b.medical else ""
        self._event(b, "ASK", f"Homeowner contacted by {channel}{sent}: “{text}”", kind="action")

    def _dispatch(self, b, tick, why):
        b.dispatched = True
        b.ladder, b.ladder_since = "DISPATCH", tick
        self.stats["trucks_sent"] += 1
        parts = " with likely parts (cell module / BMS board)" if b.label == "HARDWARE_FAILING" else ""
        self._event(b, "DISPATCH", f"{why}. Technician dispatched{parts}. Diagnosis attached.",
                    kind="dispatch")

    def homeowner_reply(self, battery_id: str, text: str, tick: int | None = None):
        tick = self.tick if tick is None else tick
        b = self.batteries.get(battery_id)
        if not b:
            return
        b.contact_log.append({"tick": tick, "dir": "in", "text": text})
        self._event(b, "REPLY", f"Homeowner: “{text}”", kind="signal")
        up = text.upper()
        if "RED" in up and b.label in ("TRIPPED", "UNKNOWN", "COMMS_LOSS") and not b.dispatched:
            self._dispatch(b, tick, "Homeowner reports a red fault light")
        elif "RESET" in up and b.label in ("COMMS_LOSS", "UNKNOWN"):
            # the homeowner's answer contradicts the guess: learn from it, out loud
            was = LABELS[b.label]["title"].lower()
            b.confidence = 0.9
            b.reasons = [f"Initial guess was {was} (battery was idle, so the meter couldn't tell)",
                         "Homeowner had to press RESET: the battery had tripped"]
            b.label, b.diag_since = "TRIPPED", tick
            self._event(b, "CORRECTED", f"Diagnosis corrected: {was} -> tripped, based on the homeowner's reply",
                        kind="decision")

    def _on_return(self, b, tick):
        prev = b.label
        if prev != "WATCH":
            msg = {"TRIPPED": "Back online after the homeowner reset it",
                   "COMMS_LOSS": "Back online -- confirmed it was only a comms loss",
                   "NETWORK_OUTAGE": "Back online as the carrier recovered",
                   "GATEWAY_FAULT": f"Back online: engineer repaired gateway {b.gateway}",
                   "GRID_OUTAGE": "Back online as the feeder was restored"}.get(prev, "Back online")
            if not b.dispatched and prev != "GATEWAY_FAULT":
                self.stats["trucks_avoided"] += 1
                self.stats["resolved_remotely"] += 1
                msg += ". Truck avoided."
            self._event(b, "RESOLVED", msg, kind="resolved")
        b.label, b.confidence, b.reasons = "OK", 1.0, []
        b.ladder, b.asked, b.dispatched, b.last_gasp = None, False, False, None

    def _event(self, b, label, text, kind="decision"):
        self._seq += 1
        self.feed.appendleft({"seq": self._seq, "tick": self.tick, "id": b.id, "label": label,
                              "kind": kind, "text": text, "confidence": round(b.confidence, 2),
                              "reasons": list(b.reasons), "medical": b.medical,
                              "feeder": b.feeder})

    # ================================================================ metrics
    def capacity_kw(self) -> dict:
        ours = naive = 0.0
        for b in self.batteries.values():
            last = self._last_live(b)
            soc = last["soc"] if last else 0
            usable = b.max_kw if soc > 20 else 0
            if b.label in ("OK", "AT_RISK", "TELEMETRY_MISMATCH", "WATCH"):
                naive += usable  # a naive system trusts anything that talks
            if b.label in ("OK", "WATCH", "COMMS_LOSS", "NETWORK_OUTAGE", "GATEWAY_FAULT"):
                ours += usable
            elif b.label == "AT_RISK":
                ours += usable * 0.5
        return {"naive": round(naive, 1), "ours": round(ours, 1)}

    def counts(self) -> dict:
        c = {k: 0 for k in LABELS}
        for b in self.batteries.values():
            c[b.label] = c.get(b.label, 0) + 1
        return c

    def detail(self, battery_id: str) -> dict | None:
        b = self.batteries.get(battery_id)
        if not b:
            return None
        return {"id": b.id, "feeder": b.feeder, "carrier": b.carrier, "medical": b.medical,
                "gateway": b.gateway,
                "label": b.label, "title": LABELS[b.label]["title"], "confidence": b.confidence,
                "reasons": b.reasons, "ladder": b.ladder, "ladder_steps": LADDER,
                "last_seen": b.last_seen, "external": b.external,
                "blackbox": list(b.blackbox)[-15:], "contact_log": b.contact_log[-6:],
                "missed": max(0, self.tick - b.last_seen - 1) if b.last_seen >= 0 else None}
