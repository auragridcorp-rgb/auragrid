"""earthquakes: global earthquake activity from the USGS real-time GeoJSON feeds.

Source: U.S. Geological Survey Earthquake Hazards Program summary feeds
(M2.5+ past day, M4.5+ past week), updated every minute. USGS data is U.S.
public domain; credit "U.S. Geological Survey". No key; one request per feed
per refresh (default every 10 minutes).
"""
from datetime import datetime, timezone

import requests

import feeds

DAY_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
WEEK_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_week.geojson"
USER_AGENT = "AurAgrid/1.0 (auragridcorp@gmail.com)"


def _iso(ms) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")


def _event(f: dict) -> dict:
    p, coords = f["properties"], f["geometry"]["coordinates"]
    return {"id": f["id"], "magnitude": p.get("mag"), "place": p.get("place"), "time": _iso(p["time"]),
            "lon": coords[0], "lat": coords[1], "depth_km": coords[2], "tsunami_flag": bool(p.get("tsunami")),
            "pager_alert": p.get("alert"), "significance": p.get("sig"), "url": p.get("url")}


def _compact(e: dict) -> dict:
    return {k: e[k] for k in ("id", "magnitude", "time", "lat", "lon", "depth_km", "tsunami_flag")}


def build_snapshot(day: dict, week: dict) -> dict:
    day_events = [_event(f) for f in day.get("features", []) if f["properties"].get("mag") is not None]
    week_events = [_event(f) for f in week.get("features", []) if f["properties"].get("mag") is not None]
    bands = {"m2_5_to_4": 0, "m4_to_5": 0, "m5_to_6": 0, "m6_plus": 0}
    for e in day_events:
        m = e["magnitude"]
        bands["m6_plus" if m >= 6 else "m5_to_6" if m >= 5 else "m4_to_5" if m >= 4 else "m2_5_to_4"] += 1
    by_mag = lambda events: sorted(events, key=lambda e: -e["magnitude"])  # noqa: E731
    generated = max(day.get("metadata", {}).get("generated", 0), week.get("metadata", {}).get("generated", 0))
    return {
        "dataset": "AurAgrid Global Earthquake Activity",
        "generated_utc": _iso(generated) if generated else None,
        "past_24h": {"count_m2_5_plus": len(day_events), "by_magnitude": bands,
                     "largest": by_mag(day_events)[:10]},
        "past_7d": {"count_m4_5_plus": len(week_events),
                    "count_m6_plus": sum(1 for e in week_events if e["magnitude"] >= 6),
                    "tsunami_flagged": [e for e in week_events if e["tsunami_flag"]],
                    "pager_alerts": [e for e in week_events if e["pager_alert"]],
                    "largest": by_mag(week_events)[:10]},
        # Compact list of every event (for area queries in derived.py): M2.5+ past day, M4.5+ past week.
        "events": {
            "m2_5_plus_24h": [_compact(e) for e in day_events],
            "m4_5_plus_7d": [_compact(e) for e in week_events],
        },
        "notes": "USGS real-time feeds; magnitudes and locations can be revised. "
                 "tsunami_flag means the event is in a tsunami-relevant area, not that a tsunami occurred.",
    }


def fetch(session=None) -> dict:
    session = session or requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    get = lambda url: session.get(url, timeout=30)  # noqa: E731
    day, week = get(DAY_URL), get(WEEK_URL)
    day.raise_for_status()
    week.raise_for_status()
    return build_snapshot(day.json(), week.json())


feeds.describe(
    "earthquakes", source="U.S. Geological Survey (USGS) Earthquake Hazards Program real-time feeds",
    url=DAY_URL, terms_url="https://www.usgs.gov/information-policies-and-instructions/copyrights-and-credits",
    attribution="Credit: U.S. Geological Survey. Public domain; USGS does not endorse AurAgrid.",
    license='U.S. public domain (USGS); credit the U.S. Geological Survey',
    as_of=lambda snap: snap["generated_utc"])
