"""Wires the simulator and the engine together. Used by the server and by the tests."""
from __future__ import annotations

from .engine import Engine, LABELS
from .regions import DEFAULT_REGION, REGIONS, public
from .sim import Simulator

SILENT = {k for k, v in LABELS.items() if v["silent"]} - {"WATCH"}


class Fleet:
    def __init__(self, n: int = 500, seed: int = 7, virtual_meters: bool = True, region: str = DEFAULT_REGION):
        self.n, self.seed, self.virtual_meters, self.region = n, seed, virtual_meters, region
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
        dispatched_now = sum(1 for b in eng.batteries.values() if b.dispatched)
        return {
            "tick": self.sim.tick, "heatwave": self.sim.heatwave,
            "feeders_out": sorted(self.sim.feeders_out), "carriers_out": sorted(self.sim.carriers_out),
            "counts": counts, "silent": silent_now,
            "capacity": {**cap, "truth": truth,
                         "err_naive": round(abs(cap["naive"] - truth), 1),
                         "err_ours": round(abs(cap["ours"] - truth), 1)},
            "trucks": {"naive": silent_now, "ours": dispatched_now, **eng.stats},
            "accuracy": self.accuracy(),
        }

    def statuses(self) -> list:
        return [[b.id, b.label, b.ladder or "", round(b.confidence, 2)]
                for b in self.engine.batteries.values()]

    def options(self) -> dict:
        return {"feeders": [f[0] for f in self.sim.feeders], "carriers": list(self.sim.carriers),
                "region": public(self.region),
                "regions": [{"key": k, "label": v["label"]} for k, v in REGIONS.items()]}
