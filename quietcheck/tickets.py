"""
Investigation tickets: every incident QuietCheck handles becomes a ticket, recorded against
each battery (house) it touched, so the history can be searched, exported and learned from.

Ticket scopes
  * battery  -- one home: tripped, comms loss, hardware failure, at risk, spoofed, unknown, heat watch
  * gateway  -- one shared neighbourhood gateway (one engineer for all homes behind it)
  * carrier  -- a cellular network outage
  * feeder   -- a grid outage on one feeder
  * fleet    -- a pre-disaster plan (heatwave)

Each ticket carries an investigation report (the evidence), the actions taken, a timeline,
and how many visits a naive system would have made compared with ours.
Tickets are kept in memory and appended to a JSON-lines log (TICKETS_LOG, default
data/tickets.jsonl) when they open and close.
"""
from __future__ import annotations

import csv
import io
import json
import os
import time
from datetime import datetime, timezone

GROUP = {"GATEWAY_FAULT": "gateway", "NETWORK_OUTAGE": "carrier", "GRID_OUTAGE": "feeder"}
PER_BATTERY = {"TRIPPED", "COMMS_LOSS", "HARDWARE_FAILING", "UNKNOWN", "AT_RISK", "TELEMETRY_MISMATCH"}
TIMELINE_EVENTS = {"ASK", "REPLY", "DISPATCH", "CORRECTED", "BACKLOG", "LAST_GASP", "RESOLVED"}

TITLES = {
    "GATEWAY_FAULT": "Shared gateway down",
    "NETWORK_OUTAGE": "Carrier outage",
    "GRID_OUTAGE": "Grid outage on feeder",
    "TRIPPED": "Battery tripped",
    "COMMS_LOSS": "Battery lost its connection",
    "HARDWARE_FAILING": "Hardware failure",
    "UNKNOWN": "Silent, cause unclear",
    "AT_RISK": "At risk: heating faster than the fleet",
    "TELEMETRY_MISMATCH": "Telemetry doesn't add up",
    "HEATWAVE_PREP": "Heatwave pre-disaster plan",
    "HEAT_WATCH": "Heat watch",
}
SEVERITY = {"HARDWARE_FAILING": "high", "AT_RISK": "high", "TELEMETRY_MISMATCH": "high",
            "HEATWAVE_PREP": "high", "GRID_OUTAGE": "high", "GATEWAY_FAULT": "medium",
            "TRIPPED": "medium", "NETWORK_OUTAGE": "medium", "UNKNOWN": "medium",
            "COMMS_LOSS": "low", "HEAT_WATCH": "low"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Tickets:
    def __init__(self, log_path: str | None = None):
        self.items: dict[str, dict] = {}
        self.by_battery: dict[str, list[str]] = {}
        self.open_key: dict[tuple, str] = {}     # (scope, key) -> open ticket id
        self._n = 0
        self._feed_seq = 0
        self._new: list[str] = []
        self.run = time.strftime("%Y%m%d-%H%M%S")
        self.log_path = log_path if log_path is not None else os.getenv("TICKETS_LOG", "data/tickets.jsonl")

    # ------------------------------------------------------------ basics
    def _open(self, kind: str, scope: str, key: str, tick: int, batteries: list[str], **extra) -> dict:
        self._n += 1
        t = {"id": f"T-{self._n:04d}", "run": self.run, "kind": kind, "title": TITLES.get(kind, kind),
             "scope": scope, "key": key, "severity": SEVERITY.get(kind, "medium"), "status": "open",
             "opened_tick": tick, "opened_at": _now(), "closed_tick": None, "closed_at": None,
             "batteries": [], "diagnosis": kind, "confidence": None, "report": [], "actions": [],
             "timeline": [], "naive_visits": 0, "our_visits": 0, "resolution": None}
        t.update(extra)
        self.items[t["id"]] = t
        self.open_key[(scope, key)] = t["id"]
        for b in batteries:
            self._attach(t, b)
        self._new.append(t["id"])
        self._log("open", t)
        return t

    def _attach(self, t: dict, bid: str) -> None:
        if bid not in t["batteries"]:
            t["batteries"].append(bid)
            self.by_battery.setdefault(bid, []).append(t["id"])

    def _close(self, t: dict, tick: int, resolution: str) -> None:
        t["status"], t["closed_tick"], t["closed_at"], t["resolution"] = "closed", tick, _now(), resolution
        t["timeline"].append({"tick": tick, "text": f"Closed: {resolution}"})
        self.open_key.pop((t["scope"], t["key"]), None)
        self._log("close", t)

    def _log(self, what: str, t: dict) -> None:
        if not self.log_path:
            return
        try:
            d = os.path.dirname(self.log_path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(json.dumps({"event": what, **t}) + "\n")
        except OSError:
            pass  # recording is best-effort; never break the dashboard

    def pop_new(self) -> list[dict]:
        out = [self.items[i] for i in self._new]
        self._new = []
        return out

    # ------------------------------------------------------------ update from the engine
    def update(self, engine, tick: int) -> None:
        groups: dict[tuple, list] = {}
        singles = {}
        for b in engine.batteries.values():
            if b.label in GROUP:
                scope = GROUP[b.label]
                key = {"gateway": b.gateway, "carrier": b.carrier, "feeder": b.feeder}[scope] or b.id
                groups.setdefault((scope, key, b.label), []).append(b)
            elif b.label in PER_BATTERY:
                singles[b.id] = b

        # group incidents: one ticket for the whole gateway / carrier / feeder
        live_groups = set()
        for (scope, key, label), members in groups.items():
            live_groups.add((scope, key))
            tid = self.open_key.get((scope, key))
            t = self.items.get(tid) if tid else None
            if t is None:
                t = self._open(label, scope, key, tick, [m.id for m in members],
                               title=f"{TITLES[label]}: {key}")
            for m in members:
                self._attach(t, m.id)
            t["confidence"] = max(m.confidence for m in members)
            t["report"] = self._group_report(engine, scope, key, members)
            t["naive_visits"] = len(t["batteries"])
            if scope == "gateway":
                sent = key in engine.gateway_dispatched
                t["our_visits"] = 1 if sent else 0
                t["actions"] = [f"Hold the capacity of all {len(t['batteries'])} batteries: they are still running",
                                f"One engineer to gateway {key}" + (" (dispatched)" if sent else ""),
                                "No visits to individual homes"]
            elif scope == "carrier":
                t["actions"] = ["Wait it out; carrier notified", "Hold the batteries' capacity", "No truck rolls"]
            else:
                t["actions"] = ["Handed to the utility", "Batteries are backing up their homes", "No truck rolls"]

        # single-battery incidents
        for bid, b in singles.items():
            tid = self.open_key.get(("battery", bid))
            t = self.items.get(tid) if tid else None
            if t is None:
                t = self._open(b.label, "battery", bid, tick, [bid], title=f"{TITLES[b.label]}: {bid}",
                               medical=b.medical, feeder=b.feeder, gateway=b.gateway)
            if t["kind"] == "HEAT_WATCH":     # a watched battery that turned bad: escalate in place
                t["timeline"].append({"tick": tick, "text": f"Escalated: {TITLES[b.label]}"})
                t["kind"], t["title"] = b.label, f"{TITLES[b.label]}: {bid}"
                t["severity"] = SEVERITY.get(b.label, "medium")
            elif t["diagnosis"] != b.label:
                t["timeline"].append({"tick": tick, "text": f"Diagnosis changed to {TITLES[b.label].lower()}"})
            t["diagnosis"], t["confidence"] = b.label, b.confidence
            t["report"] = list(b.reasons)
            t["naive_visits"] = 1
            t["our_visits"] = 1 if b.dispatched else 0
            t["actions"] = [e for e in [self._last_action(engine, bid)] if e]

        # close what is over
        for (scope, key), tid in list(self.open_key.items()):
            t = self.items[tid]
            if t["kind"] in ("HEATWAVE_PREP", "HEAT_WATCH"):
                continue
            if scope == "battery":
                b = engine.batteries.get(key)
                if key in singles or (b is not None and b.label == "WATCH"):
                    continue
                self._close(t, tick, self._resolution(engine, key) or "Back to healthy")
            elif (scope, key) not in live_groups:
                self._close(t, tick, f"All {len(t['batteries'])} batteries back online")

        # timeline: copy the engine's actions and replies onto the right tickets
        for e in sorted(engine.feed, key=lambda e: e["seq"]):
            if e["seq"] <= self._feed_seq:
                continue
            self._feed_seq = e["seq"]
            if e["label"] not in TIMELINE_EVENTS:
                continue
            bid = e["id"]
            for tid in self.by_battery.get(bid, []):
                t = self.items[tid]
                if t["status"] != "open":
                    continue
                if t["scope"] == "battery" or e["label"] == "DISPATCH":
                    t["timeline"].append({"tick": e["tick"], "text": e["text"]})

    def _last_action(self, engine, bid):
        for e in engine.feed:
            if e["id"] == bid and e["kind"] in ("decision", "action", "dispatch"):
                return e["text"]
        return None

    def _resolution(self, engine, bid):
        for e in engine.feed:
            if e["id"] == bid and e["label"] in ("RESOLVED", "OK"):
                return e["text"]
        return None

    def _group_report(self, engine, scope, key, members):
        n = len(members)
        if scope == "gateway":
            gs = getattr(engine, "_gw_stats", {}).get(key, {})
            carriers = sorted({m.carrier for m in members})
            feeders = sorted({m.feeder for m in members})
            rep = [f"{gs.get('silent', n)}/{gs.get('total', n)} batteries behind {key} stopped reporting "
                   f"within {gs.get('spread', 0) + 1} check-in(s) of each other",
                   "Every one of their smart meters still reports: the batteries are running, only comms are lost",
                   f"They use {len(carriers)} carrier(s) ({', '.join(carriers)}) and the rest of each carrier is fine",
                   f"Neighbouring gateways on {', '.join(feeders)} are reporting normally",
                   f"Conclusion: the shared gateway {key} failed. 1 engineer instead of {n} home visits"]
            return rep
        if scope == "carrier":
            return [f"{n} batteries on {key} went quiet together while their meters kept reporting",
                    "Pattern matches a carrier outage, not separate battery faults"]
        return [f"{n} batteries on {key} went quiet and their smart meters went dark too",
                "The feeder lost grid power; batteries are islanding their homes"]

    # ------------------------------------------------------------ heatwave plan
    def open_heat_plan(self, tick: int, plan: dict) -> dict:
        t = self._open("HEATWAVE_PREP", "fleet", "heatwave", tick, [], title="Heatwave pre-disaster plan",
                       plan=plan)
        t["report"] = [f"Heatwave declared: ambient {plan['ambient_c']:.0f} C, every battery runs hotter",
                       f"Fleet median battery temperature {plan['fleet_temp_c']:.1f} C",
                       f"{len(plan['precharge'])} batteries below {plan['precharge_below']:.0f}% charge, "
                       f"{len(plan['medical'])} homes with medical devices",
                       f"{len(plan['watch'])} batteries already running {plan['watch_margin']:.0f}+ C hotter than the fleet"]
        t["actions"] = [f"Pre-charge {len(plan['precharge'])} batteries to {plan['precharge_to']:.0f}% before the peak",
                        f"Top up {len(plan['medical'])} medical-device homes to 100% and call each one",
                        f"Heat watch on {len(plan['watch'])} batteries: discharge capped at {plan['derate_kw']:.0f} kW",
                        "At-risk alarms judged against the fleet, so the heat itself raises no false alarms"]
        t["naive_visits"] = 0
        t["timeline"].append({"tick": tick, "text": "Plan started automatically when the heatwave was declared"})
        for w in plan["watch"]:
            wt = self._open("HEAT_WATCH", "battery", w["id"], tick, [w["id"]], title=f"Heat watch: {w['id']}",
                            parent=t["id"], medical=w.get("medical", False))
            wt["report"] = [f"Running {w['temp_c']:.1f} C, {w['excess_c']:.1f} C above the fleet median",
                            f"Discharging {w['kw']:.1f} kW at the start of the heatwave"]
            wt["actions"] = [f"Discharge capped at {plan['derate_kw']:.0f} kW for the heatwave",
                             "Temperature trend checked on every check-in"]
            self._attach(t, w["id"])
        return t

    def close_heat_plan(self, engine, tick: int, progress: dict) -> None:
        tid = self.open_key.get(("fleet", "heatwave"))
        if not tid:
            return
        t = self.items[tid]
        failed = 0
        for (scope, key), wid in list(self.open_key.items()):
            w = self.items[wid]
            if w["kind"] == "HEAT_WATCH" and w.get("parent") == tid:
                b = engine.batteries.get(key)
                self._close(w, tick, "Heatwave over; no fault developed" if b and b.label == "OK"
                            else f"Heatwave over; battery is {b.label if b else 'gone'}")
        for bid in t["batteries"]:
            b = engine.batteries.get(bid)
            if b and b.label in ("AT_RISK", "HARDWARE_FAILING"):
                failed += 1
        self._close(t, tick, f"Heatwave over. Pre-charged {progress.get('done', 0)}/{progress.get('total', 0)} "
                             f"batteries; {failed} heat-watch battery failures")

    def heat_progress(self, tick: int, text: str) -> None:
        tid = self.open_key.get(("fleet", "heatwave"))
        if tid:
            tl = self.items[tid]["timeline"]
            if not tl or tl[-1]["text"] != text:
                tl.append({"tick": tick, "text": text})

    # ------------------------------------------------------------ queries
    def list(self, battery: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
        ids = self.by_battery.get(battery, []) if battery else list(self.items)
        out = [self.items[i] for i in ids]
        if status:
            out = [t for t in out if t["status"] == status]
        out.sort(key=lambda t: (t["status"] != "open", -t["opened_tick"]))
        return out[:limit]

    def summary(self) -> dict:
        open_ = [t for t in self.items.values() if t["status"] == "open"]
        return {"open": len(open_), "total": len(self.items),
                "visits_naive": sum(t["naive_visits"] for t in self.items.values()),
                "visits_ours": sum(t["our_visits"] for t in self.items.values())}

    def csv(self, sim=None) -> str:
        """One row per ticket per battery (house), for analysis."""
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["ticket", "battery", "kind", "scope", "group", "severity", "status", "opened_tick",
                    "closed_tick", "opened_at", "closed_at", "feeder", "gateway", "carrier", "medical",
                    "confidence", "naive_visits", "our_visits", "resolution"])
        info = {}
        if sim is not None:
            info = {b.id: b for b in sim.batteries.values()}
        for t in sorted(self.items.values(), key=lambda t: t["id"]):
            for bid in t["batteries"] or [""]:
                b = info.get(bid)
                w.writerow([t["id"], bid, t["kind"], t["scope"], t["key"], t["severity"], t["status"],
                            t["opened_tick"], t["closed_tick"], t["opened_at"], t["closed_at"],
                            getattr(b, "feeder", ""), getattr(b, "gateway", ""), getattr(b, "carrier", ""),
                            getattr(b, "medical", ""), t["confidence"], t["naive_visits"], t["our_visits"],
                            t["resolution"] or ""])
        return buf.getvalue()
