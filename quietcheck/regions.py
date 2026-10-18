"""
Region presets (Twist 2: make it global).

The diagnosis logic is the same everywhere: a silent battery is judged by the signals that
are still talking. What changes from city to city is *which* signals exist and how you reach
people: smart-meter coverage, the cellular carriers, the messaging app homeowners answer,
the grid's voltage and frequency, and the hazard that makes silence dangerous.

Feeder names are real districts; coordinates are approximate district centres.
"""

REGIONS = {
    "la": {
        "label": "Los Angeles, USA",
        "grid": "60 Hz · 120/240 V split-phase",
        "meter_coverage": 1.0,
        "carriers": ["Verizon", "T-Mobile", "AT&T"],
        "carrier_weights": [45, 33, 22],
        "channel": "SMS", "language": "English",
        "hazard": "Wildfire power shutoffs and heatwaves",
        "center": [34.045, -118.29], "zoom": 12, "spread": [0.011, 0.013],
        "feeders": [
            ("F01 University Park", 34.0224, -118.2851), ("F02 Koreatown", 34.0618, -118.3005),
            ("F03 Silver Lake", 34.0869, -118.2702), ("F04 Highland Park", 34.1115, -118.1923),
            ("F05 Boyle Heights", 34.0339, -118.2050), ("F06 Leimert Park", 34.0050, -118.3320),
            ("F07 Inglewood", 33.9617, -118.3531), ("F08 Culver City", 34.0211, -118.3965),
            ("F09 Mid-City", 34.0420, -118.3480), ("F10 Hollywood", 34.0980, -118.3270),
            ("F11 Eagle Rock", 34.1390, -118.2120), ("F12 South Park", 33.9870, -118.2700),
        ],
    },
    "shenzhen": {
        "label": "Shenzhen, China",
        "grid": "50 Hz · 220 V",
        "meter_coverage": 1.0,
        "carriers": ["China Mobile", "China Telecom", "China Unicom"],
        "carrier_weights": [60, 22, 18],
        "channel": "WeChat", "language": "Mandarin",
        "hazard": "Typhoons and summer peak-load shortages",
        "center": [22.62, 114.04], "zoom": 11, "spread": [0.014, 0.016],
        "feeders": [
            ("F01 Futian", 22.541, 114.055), ("F02 Nanshan", 22.531, 113.930),
            ("F03 Luohu", 22.548, 114.131), ("F04 Bao'an", 22.555, 113.884),
            ("F05 Longgang", 22.720, 114.247), ("F06 Longhua", 22.657, 114.045),
            ("F07 Yantian", 22.556, 114.236), ("F08 Guangming", 22.749, 113.936),
        ],
    },
    "mumbai": {
        "label": "Mumbai, India",
        "grid": "50 Hz · 230 V",
        "meter_coverage": 0.25,
        "carriers": ["Jio", "Airtel", "Vi"],
        "carrier_weights": [48, 36, 16],
        "channel": "WhatsApp", "language": "Hindi / Marathi",
        "hazard": "Monsoon flooding and load-shedding",
        "center": [19.07, 72.87], "zoom": 11, "spread": [0.010, 0.010],
        "feeders": [
            ("F01 Colaba", 18.906, 72.815), ("F02 Dadar", 19.018, 72.844),
            ("F03 Bandra", 19.060, 72.836), ("F04 Andheri", 19.119, 72.847),
            ("F05 Kurla", 19.072, 72.882), ("F06 Ghatkopar", 19.086, 72.908),
            ("F07 Borivali", 19.231, 72.857), ("F08 Powai", 19.118, 72.906),
        ],
    },
    "tokyo": {
        "label": "Tokyo, Japan",
        "grid": "50 Hz · 100 V",
        "meter_coverage": 1.0,
        "carriers": ["docomo", "au (KDDI)", "SoftBank"],
        "carrier_weights": [42, 31, 27],
        "channel": "LINE", "language": "Japanese",
        "hazard": "Earthquakes and typhoons",
        "center": [35.68, 139.72], "zoom": 11, "spread": [0.010, 0.012],
        "feeders": [
            ("F01 Shinjuku", 35.694, 139.703), ("F02 Shibuya", 35.662, 139.704),
            ("F03 Setagaya", 35.646, 139.653), ("F04 Nerima", 35.736, 139.652),
            ("F05 Adachi", 35.775, 139.804), ("F06 Koto", 35.673, 139.817),
            ("F07 Ota", 35.561, 139.716), ("F08 Suginami", 35.699, 139.636),
        ],
    },
    "adelaide": {
        "label": "Adelaide, Australia",
        "grid": "50 Hz · 230 V",
        "meter_coverage": 0.9,
        "carriers": ["Telstra", "Optus", "Vodafone"],
        "carrier_weights": [50, 30, 20],
        "channel": "SMS", "language": "English",
        "hazard": "Bushfires and extreme heat",
        "center": [-34.91, 138.59], "zoom": 11, "spread": [0.012, 0.014],
        "feeders": [
            ("F01 Adelaide CBD", -34.928, 138.600), ("F02 Glenelg", -34.980, 138.515),
            ("F03 Prospect", -34.884, 138.603), ("F04 Unley", -34.950, 138.607),
            ("F05 Port Adelaide", -34.847, 138.507), ("F06 Salisbury", -34.758, 138.643),
            ("F07 Marion", -35.010, 138.556), ("F08 Burnside", -34.938, 138.648),
        ],
    },
}

DEFAULT_REGION = "la"


def public(region_key: str) -> dict:
    r = REGIONS[region_key]
    return {"key": region_key, **{k: v for k, v in r.items() if k not in ("feeders", "carrier_weights")}}
