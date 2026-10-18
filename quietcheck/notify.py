"""
Real phone alerts for real devices (the ESP32), via ntfy.sh push notifications.

Free, no account: install the "ntfy" app on your phone, subscribe to a topic name,
and set the same name in the NTFY_TOPIC environment variable on the server.
Only real devices alert (not the 500 simulated batteries), and only when their
diagnosis changes.
"""
from __future__ import annotations

import os
import threading
import urllib.request

TOPIC = os.getenv("NTFY_TOPIC", "").strip()
SERVER = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")

MESSAGES = {
    "TRIPPED": ("Your battery tripped",
                "{id} tripped its breaker and sent a last gasp. Press RESET on the unit. "
                "No technician needed.", "high", "warning"),
    "COMMS_LOSS": ("Your battery lost its connection",
                   "{id} stopped checking in, but your meter shows it is still powering the home. "
                   "Probably your Wi-Fi. No action needed.", "default", "signal_strength"),
    "NETWORK_OUTAGE": ("Network outage nearby",
                       "{id} went quiet with others on the same network. We'll wait it out.",
                       "default", "satellite"),
    "GRID_OUTAGE": ("Power outage on your street",
                    "Your whole feeder is down. Your utility has been told. {id} is fine.",
                    "default", "electric_plug"),
    "HARDWARE_FAILING": ("Battery needs a technician",
                         "{id} shows a real hardware fault. A technician is being booked.",
                         "urgent", "rotating_light"),
    "AT_RISK": ("Battery at risk",
                "{id} is heating faster than other batteries. Discharge capped at 50%, "
                "inspection booked.", "high", "fire"),
    "TELEMETRY_MISMATCH": ("Battery data looks wrong",
                           "{id} reports numbers your meter doesn't agree with.", "high", "mag"),
    "OK": ("Battery back to normal", "{id} is checking in and healthy again.", "low", "white_check_mark"),
}


def _send(title: str, body: str, priority: str, tag: str) -> None:
    try:
        req = urllib.request.Request(f"{SERVER}/{TOPIC}", data=body.encode(), method="POST",
                                     headers={"Title": title, "Priority": priority, "Tags": tag})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:  # alerts are best-effort; never break the server
        print("ntfy send failed:", e)


class Notifier:
    def __init__(self):
        self.last: dict[str, str] = {}

    def check(self, engine) -> None:
        if not TOPIC:
            return
        for b in engine.batteries.values():
            if not b.external:
                continue
            prev = self.last.get(b.id)
            self.last[b.id] = b.label
            if prev is None:            # first sighting: remember, don't alert
                continue
            if b.label == prev or b.label not in MESSAGES or b.label == "WATCH":
                continue
            if b.label == "OK" and prev == "WATCH":
                continue                # a single blip that recovered: stay quiet
            title, body, prio, tag = MESSAGES[b.label]
            threading.Thread(target=_send, args=(title, body.format(id=b.id), prio, tag),
                             daemon=True).start()

    def alarms(self, alarms: list) -> None:
        """Operator alerts (heatwave plan, gateway down): push each new alarm once."""
        if not TOPIC:
            return
        for a in alarms:
            key = f"alarm-{a['id']}-{a['tick']}-{a['title']}"
            if key in self.last:
                continue
            self.last[key] = "sent"
            prio, tag = ("high", "thermometer") if a["level"] == "warning" else ("default", "satellite_antenna")
            body = "\n".join(a["lines"][:3])
            threading.Thread(target=_send, args=(a["title"], body, prio, tag), daemon=True).start()
