"""Inject every fault and check the engine names the right cause.  Run: python -m unittest -v"""
import unittest

from quietcheck.fleet import Fleet


def run(fleet, ticks):
    for _ in range(ticks):
        fleet.step()


def pick_discharging(fleet):
    for b in fleet.sim.batteries.values():
        if b.setpoint_kw >= 2.5 and b.medical is False:
            return b.id
    raise RuntimeError("no discharging battery")


class FaultTests(unittest.TestCase):
    def setUp(self):
        self.f = Fleet(n=500, seed=7)
        self.f.sim.blip_rate = 0.0

    def label(self, bid):
        return self.f.engine.batteries[bid].label

    def test_comms_loss(self):
        bid = pick_discharging(self.f)
        self.f.inject("comms_loss", bid)
        run(self.f, 5)
        self.assertEqual(self.label(bid), "COMMS_LOSS")
        self.assertFalse(self.f.engine.batteries[bid].dispatched)

    def test_trip_with_last_gasp_resolves_without_truck(self):
        bid = pick_discharging(self.f)
        self.f.inject("trip", bid)
        run(self.f, 4)
        self.assertEqual(self.label(bid), "TRIPPED")
        run(self.f, 8)  # homeowner presses reset
        self.assertEqual(self.label(bid), "OK")
        self.assertGreaterEqual(self.f.engine.stats["trucks_avoided"], 1)
        self.assertEqual(self.f.engine.stats["trucks_sent"], 0)

    def test_trip_without_last_gasp_uses_meter(self):
        bid = pick_discharging(self.f)
        self.f.inject("trip_silent", bid)
        run(self.f, 5)
        decided = [e["label"] for e in self.f.engine.feed if e["id"] == bid and e["kind"] == "decision"]
        self.assertIn("TRIPPED", decided)

    def test_hardware_failure_seen_in_black_box(self):
        bid = pick_discharging(self.f)
        self.f.inject("hw_fail", bid)
        run(self.f, 3)
        self.assertEqual(self.label(bid), "AT_RISK")  # pre-emptive warning while still talking
        run(self.f, 8)
        self.assertEqual(self.label(bid), "HARDWARE_FAILING")
        self.assertTrue(self.f.engine.batteries[bid].dispatched)

    def test_feeder_outage(self):
        self.f.inject("feeder_outage", "F03 Silver Lake")
        run(self.f, 5)
        labels = [b.label for b in self.f.engine.batteries.values() if b.feeder == "F03 Silver Lake"]
        self.assertTrue(all(l == "GRID_OUTAGE" for l in labels), set(labels))
        self.assertEqual(self.f.engine.stats["trucks_sent"], 0)

    def test_carrier_outage(self):
        self.f.inject("carrier_outage", "T-Mobile")
        run(self.f, 5)
        labels = [b.label for b in self.f.engine.batteries.values() if b.carrier == "T-Mobile"]
        self.assertGreater(labels.count("NETWORK_OUTAGE") / len(labels), 0.95)
        m = self.f.metrics()
        # naive capacity collapses, ours stays close to the truth
        self.assertLess(m["capacity"]["err_ours"], m["capacity"]["err_naive"])

    def test_spoofed_telemetry(self):
        bid = pick_discharging(self.f)
        self.f.inject("spoof", bid)
        run(self.f, 7)
        self.assertEqual(self.label(bid), "TELEMETRY_MISMATCH")

    def test_single_blip_is_ignored(self):
        f = Fleet(n=500, seed=3)
        f.sim.blip_rate = 0.05
        run(f, 20)
        self.assertEqual(f.engine.counts()["COMMS_LOSS"], 0)
        self.assertGreater(f.engine.stats["blips_ignored"], 0)

    def test_mixed_scenario_accuracy(self):
        f = self.f
        f.inject("carrier_outage", "AT&T")
        f.inject("feeder_outage", "F07 Inglewood")
        for _ in range(4):
            f.inject("comms_loss"); f.inject("trip"); f.inject("hw_fail")
        run(f, 9)
        acc = f.accuracy()
        self.assertGreaterEqual(acc["pct"], 95, acc["misses"])

    def test_external_device_and_backlog(self):
        e = self.f.engine
        t = self.f.sim.tick
        for i in range(4):
            e.ingest_checkin({"id": "ESP32-01", "soc": 80, "power_kw": 2.0, "mode": "DISCHARGE",
                              "temp_c": 30, "voltage": 398}, t)
        run(self.f, 4)
        self.assertEqual(self.label("ESP32-01"), "COMMS_LOSS")
        backlog = [{"tick": t + i, "soc": 79 - i * 0.2, "power_kw": 2.0, "mode": "DISCHARGE",
                    "temp_c": 30, "voltage": 398} for i in range(4)]
        e.ingest_backlog("ESP32-01", backlog)
        e.ingest_checkin({"id": "ESP32-01", "soc": 78, "power_kw": 2.0, "mode": "DISCHARGE",
                          "temp_c": 30, "voltage": 398}, self.f.sim.tick)
        self.assertEqual(self.label("ESP32-01"), "OK")

    def test_external_device_last_gasp(self):
        e = self.f.engine
        for _ in range(3):
            e.ingest_checkin({"id": "ESP32-02", "soc": 80, "power_kw": 3.0, "mode": "DISCHARGE",
                              "temp_c": 30, "voltage": 398}, self.f.sim.tick)
            run(self.f, 1)
        e.ingest_last_gasp({"id": "ESP32-02", "soc": 80, "power_kw": 0, "mode": "IDLE",
                            "temp_c": 31, "voltage": 398, "fault": "TRIP"}, self.f.sim.tick)
        run(self.f, 4)
        self.assertEqual(self.label("ESP32-02"), "TRIPPED")

    def test_heatwave_is_weather_not_failure(self):
        self.f.inject("heatwave")
        run(self.f, 8)
        counts = self.f.engine.counts()
        self.assertEqual(counts["AT_RISK"], 0)
        self.assertEqual(counts["HARDWARE_FAILING"], 0)
        bid = pick_discharging(self.f)
        self.f.inject("hw_fail", bid)
        run(self.f, 10)
        self.assertEqual(self.label(bid), "HARDWARE_FAILING")


class GatewayAndTicketTests(unittest.TestCase):
    def setUp(self):
        self.f = Fleet(n=500, seed=7, tickets_log="")
        self.f.sim.blip_rate = 0.0

    def test_gateway_fault_sends_one_engineer(self):
        f = self.f
        f.inject("gateway_fail", "GW-F03-2")
        run(f, 5)
        homes = [b for b in f.engine.batteries.values() if b.gateway == "GW-F03-2"]
        self.assertTrue(all(b.label == "GATEWAY_FAULT" for b in homes), {b.label for b in homes})
        self.assertEqual(f.engine.stats["trucks_sent"], 1)
        t = [t for t in f.tickets.list() if t["kind"] == "GATEWAY_FAULT"][0]
        self.assertEqual((t["naive_visits"], t["our_visits"]), (len(homes), 1))
        run(f, 15)  # the engineer repairs it
        self.assertTrue(all(b.label == "OK" for b in homes))
        self.assertEqual(f.tickets.items[t["id"]]["status"], "closed")

    def test_lone_wifi_loss_is_not_a_gateway_fault(self):
        bid = pick_discharging(self.f)
        self.f.inject("comms_loss", bid)
        run(self.f, 5)
        self.assertEqual(self.f.engine.batteries[bid].label, "COMMS_LOSS")

    def test_tickets_recorded_per_house(self):
        bid = pick_discharging(self.f)
        self.f.inject("trip", bid)
        run(self.f, 14)
        hist = self.f.tickets.list(battery=bid)
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "closed")
        self.assertIn(bid, self.f.tickets.csv(self.f.sim))

    def test_heatwave_plan_acts_before_failures(self):
        f = self.f
        f.inject("heatwave")
        run(f, 3)
        plan = [t for t in f.tickets.list() if t["kind"] == "HEATWAVE_PREP"]
        self.assertEqual(len(plan), 1)
        self.assertGreater(len(f.heat_plan["precharge"]), 0)
        self.assertTrue(any(a["level"] == "warning" for a in f.alarms))
        self.assertEqual(f.engine.counts()["AT_RISK"], 0)
        f.inject("heatwave")
        run(f, 2)
        self.assertEqual(f.tickets.items[plan[0]["id"]]["status"], "closed")


class RegionTests(unittest.TestCase):
    def test_every_region_handles_outages(self):
        from quietcheck.regions import REGIONS
        for key in REGIONS:
            f = Fleet(n=500, seed=7, region=key)
            f.sim.blip_rate = 0.0
            f.inject("carrier_outage", f.sim.carriers[0])
            f.inject("feeder_outage", f.sim.feeders[2][0])
            run(f, 6)
            self.assertGreaterEqual(f.accuracy()["pct"], 95, key)
            self.assertEqual(f.engine.stats["trucks_sent"], 0, key)

    def test_low_meter_coverage_asks_instead_of_guessing(self):
        f = Fleet(n=500, seed=7, region="mumbai")
        f.sim.blip_rate = 0.0
        bid = next(b.id for b in f.sim.batteries.values()
                   if not b.has_meter and b.setpoint_kw > 1.5 and not b.medical)
        f.inject("trip_silent", bid)
        run(f, 16)
        mine = [e for e in f.engine.feed if e["id"] == bid]
        self.assertTrue(any(e["label"] == "UNKNOWN" for e in mine))
        self.assertTrue(any("WhatsApp" in e["text"] for e in mine))
        self.assertEqual(f.engine.batteries[bid].label, "OK")
        self.assertEqual(f.engine.stats["trucks_sent"], 0)


if __name__ == "__main__":
    unittest.main()
