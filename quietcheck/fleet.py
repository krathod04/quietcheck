"""Wires the simulator and the engine together. Used by the server and by the tests."""
from __future__ import annotations

from .engine import Engine, LABELS
from .regions import DEFAULT_REGION, REGIONS, public
from .sim import Simulator
from .tickets import Tickets

HEAT = {"precharge_below": 50.0, "precharge_to": 90.0, "watch_margin": 4.0, "derate_kw": 3.0, "watch_max": 15}

SILENT = {k for k, v in LABELS.items() if v["silent"]} - {"WATCH"}


class Fleet:
    def __init__(self, n: int = 500, seed: int = 7, virtual_meters: bool = True, region: str = DEFAULT_REGION,
                 tickets_log: str | None = None):
        self.n, self.seed, self.virtual_meters, self.region = n, seed, virtual_meters, region
        self.tickets_log = tickets_log
        self.reset()

    def reset(self, region: str | None = None):
        if region in REGIONS:
            self.region = region
        self.sim = Simulator(n=self.n, seed=self.seed, region=self.region)
        self.engine = Engine()
        reg = REGIONS[self.region]
        self.engine.contact = {"channel": reg["channel"], "language": reg["language"]}
        for info in self.sim.static_info():
            self.engine.register(info)
        self.history: list[dict] = []
        self.tickets = Tickets(log_path=self.tickets_log)
        self.alarms: list[dict] = []
        self.heat_plan: dict | None = None
        self._alarm_n = 0
        # warm up so every battery has a black box and every meter a baseline
        for _ in range(6):
            self.step()

    def step(self) -> None:
        msgs = self.sim.step()
        t = msgs["tick"]
        eng = self.engine
        eng.tick = t
        for m in msgs["meters"]:
            eng.ingest_meter(m, t)
        for g in msgs["gasps"]:
            eng.ingest_last_gasp(g, t)
        for r in msgs["replies"]:
            eng.homeowner_reply(r["id"], r["text"], t)
        for c in msgs["checkins"]:
            eng.ingest_checkin(c, t)
        # external devices (ESP32) check in through the API between ticks; give each a
        # simulated smart meter so the engine can cross-check it like any other home
        if self.virtual_meters:
            for b in eng.batteries.values():
                if b.external and b.blackbox:
                    last = b.last
                    stopped = bool(b.last_gasp) or (last and last.get("fault"))
                    live = eng._last_live(b)
                    kw = 0.0 if stopped or not live else float(live.get("power_kw", 0) or 0)
                    eng.ingest_meter({"id": b.id, "import_kw": round(1.8 - kw, 2)}, t)
        eng.evaluate(t, heatwave=self.sim.heatwave)
        for o in eng.outbox:
            self.sim.homeowner_contact(o["id"], o["kind"])
        eng.outbox.clear()
        for w in eng.work_orders:                     # one engineer per failed gateway
            self.sim.dispatch_gateway(w["gateway"])
        eng.work_orders.clear()
        self._heat_step(t)
        self.tickets.update(eng, t)
        for tk in self.tickets.pop_new():
            if tk["kind"] == "GATEWAY_FAULT":
                self._alarm("info", f"Gateway {tk['key']} down",
                            [f"{len(tk['batteries'])} homes silent at once; their batteries are still running",
                             "Investigation: shared gateway failure, not separate battery faults",
                             f"1 engineer dispatched instead of {len(tk['batteries'])} home visits"], tk["id"])
        self.history.append(self._capacity_point(t))
        self.history = self.history[-150:]

    def inject(self, kind: str, target: str | None = None) -> str:
        desc = self.sim.inject(kind, target)
        self.engine._seq += 1
        self.engine.feed.appendleft({"seq": self.engine._seq, "tick": self.sim.tick, "id": "SIM",
                                     "label": "INJECT", "kind": "inject", "text": desc,
                                     "confidence": 1, "reasons": [], "medical": False,
                                     "feeder": ""})
        return desc

    # ------------------------------------------------------------ heatwave: act before the disaster
    def _heat_step(self, t):
        eng, sim = self.engine, self.sim
        if sim.heatwave and self.heat_plan is None:
            live = [b for b in eng.batteries.values() if not b.external and b.label == "OK" and b.blackbox]
            temps = sorted(eng._last_live(b)["temp_c"] for b in live)
            med = temps[len(temps) // 2] if temps else 26.0
            pre, medical, watch = [], [], []
            for b in live:
                r = eng._last_live(b)
                if b.medical:
                    medical.append(b.id); sim.precharge(b.id, 100.0)
                elif r["soc"] < HEAT["precharge_below"]:
                    pre.append(b.id); sim.precharge(b.id, HEAT["precharge_to"])
                ex = r["temp_c"] - med
                if ex >= HEAT["watch_margin"]:
                    watch.append({"id": b.id, "temp_c": r["temp_c"], "excess_c": ex, "kw": r["power_kw"],
                                  "medical": b.medical})
            watch = sorted(watch, key=lambda w: -w["excess_c"])[:HEAT["watch_max"]]
            for w in watch:
                sim.derate(w["id"], HEAT["derate_kw"])
            self.heat_plan = {"tick": t, "ambient_c": 42.0, "fleet_temp_c": med, "precharge": pre,
                              "medical": medical, "watch": watch, **HEAT}
            tk = self.tickets.open_heat_plan(t, self.heat_plan)
            self._alarm("warning", "Heatwave warning: 42 °C across the fleet",
                        [f"Pre-charging {len(pre)} batteries to {HEAT['precharge_to']:.0f}% before the evening peak",
                         f"Topping up {len(medical)} medical-device homes to 100% and calling each one",
                         f"Heat watch on {len(watch)} batteries running hotter than the fleet (capped at "
                         f"{HEAT['derate_kw']:.0f} kW)",
                         "No false alarms: overheating is judged against the whole fleet"], tk["id"])
        elif sim.heatwave and self.heat_plan:
            p = self.heat_plan
            ids = p["precharge"] + p["medical"]
            done = sum(1 for i in ids if sim.batteries[i].soc >= sim.batteries[i].precharge_to - 0.5)
            p["progress"] = {"done": done, "total": len(ids)}
            if (t - p["tick"]) % 5 == 0:
                self.tickets.heat_progress(t, f"Pre-charge progress: {done}/{len(ids)} batteries at target")
        elif not sim.heatwave and self.heat_plan:
            self.tickets.close_heat_plan(self.engine, t, self.heat_plan.get("progress", {}))
            sim.clear_heat_plan()
            self.heat_plan = None

    def _alarm(self, level, title, lines, ticket=None):
        self._alarm_n += 1
        self.alarms.append({"id": self._alarm_n, "level": level, "title": title, "lines": lines,
                            "ticket": ticket, "tick": self.sim.tick, "acked": False})
        self.alarms = self.alarms[-20:]

    def ack_alarm(self, alarm_id: int) -> None:
        for a in self.alarms:
            if a["id"] == alarm_id:
                a["acked"] = True

    def gateway_points(self) -> list[dict]:
        return list(self.sim.gateways.values())

    # ------------------------------------------------------------ metrics
    def _capacity_point(self, t):
        cap = self.engine.capacity_kw()
        return {"tick": t, "truth": self.sim.truth_capacity_kw(), **cap}

    def accuracy(self) -> dict:
        """Score engine labels against simulator truth for every non-OK battery."""
        correct = total = 0
        confusion = []
        for b in self.engine.batteries.values():
            sb = self.sim.batteries.get(b.id)
            if sb is None:
                continue
            truth = self.sim.truth(sb)
            if b.label == "WATCH":
                continue  # still debouncing
            if truth == "OK" and b.label == "OK":
                continue
            total += 1
            if truth == b.label:
                correct += 1
            else:
                confusion.append({"id": b.id, "truth": truth, "engine": b.label})
        return {"correct": correct, "total": total,
                "pct": round(100 * correct / total, 1) if total else 100.0,
                "misses": confusion[:10]}

    def metrics(self) -> dict:
        eng = self.engine
        counts = eng.counts()
        silent_now = sum(counts[k] for k in SILENT)
        cap = eng.capacity_kw()
        truth = self.sim.truth_capacity_kw()
        dispatched_now = (sum(1 for b in eng.batteries.values() if b.dispatched)
                          + len(eng.gateway_dispatched))
        return {
            "tick": self.sim.tick, "heatwave": self.sim.heatwave,
            "feeders_out": sorted(self.sim.feeders_out), "carriers_out": sorted(self.sim.carriers_out),
            "counts": counts, "silent": silent_now,
            "capacity": {**cap, "truth": truth,
                         "err_naive": round(abs(cap["naive"] - truth), 1),
                         "err_ours": round(abs(cap["ours"] - truth), 1)},
            "trucks": {"naive": silent_now, "ours": dispatched_now, **eng.stats},
            "accuracy": self.accuracy(),
            "tickets": self.tickets.summary(),
            "heat_plan": ({"precharge": len(self.heat_plan["precharge"]), "medical": len(self.heat_plan["medical"]),
                           "watch": len(self.heat_plan["watch"]), **self.heat_plan.get("progress", {})}
                          if self.heat_plan else None),
            "gateways_down": sorted(eng.gateway_dispatched),
        }

    def statuses(self) -> list:
        return [[b.id, b.label, b.ladder or "", round(b.confidence, 2)]
                for b in self.engine.batteries.values()]

    def options(self) -> dict:
        return {"feeders": [f[0] for f in self.sim.feeders], "carriers": list(self.sim.carriers),
                "region": public(self.region),
                "regions": [{"key": k, "label": v["label"]} for k, v in REGIONS.items()]}
