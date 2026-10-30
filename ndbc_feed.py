"""maritime-weather provider: latest marine observations from NOAA's NDBC.

Source: National Data Buoy Center (NOAA) latest observations file, updated
continuously, covering ~850 moored buoys, coastal (C-MAN) and partner stations
worldwide: wind, gusts, significant wave height, wave period and direction,
pressure, air/sea temperature. NOAA data is U.S. public domain; NDBC is credited.

No API key. One small text file per refresh (plus the station list once a day);
responses are cached for AURAGRID_NDBC_CACHE_SECONDS (default 10 min).
"""
import logging
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

import feeds

log = logging.getLogger("auragrid.ndbc")

OBS_URL = "https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt"
STATIONS_URL = "https://www.ndbc.noaa.gov/activestations.xml"
USER_AGENT = "AurAgrid/1.0 (auragridcorp@gmail.com)"
STATIONS_MAX_AGE = 24 * 3600

# column -> (output field, type); units per the file's second header line
COLUMNS = {
    "WDIR": ("wind_dir_deg", float), "WSPD": ("wind_speed_ms", float), "GST": ("gust_ms", float),
    "WVHT": ("wave_height_m", float), "DPD": ("dominant_wave_period_s", float),
    "APD": ("average_wave_period_s", float), "MWD": ("wave_dir_deg", float),
    "PRES": ("pressure_hpa", float), "PTDY": ("pressure_tendency_hpa", float),
    "ATMP": ("air_temp_c", float), "WTMP": ("water_temp_c", float), "DEWP": ("dewpoint_c", float),
    "VIS": ("visibility_nmi", float), "TIDE": ("tide_ft", float),
}


def parse_observations(text: str) -> list:
    lines = text.splitlines()
    header = lines[0].lstrip("#").split()
    rows = []
    for line in lines[1:]:
        if not line.strip() or line.startswith("#"):
            continue
        cells = dict(zip(header, line.split()))
        try:
            observed = datetime(int(cells["YYYY"]), int(cells["MM"]), int(cells["DD"]),
                                int(cells["hh"]), int(cells["mm"]), tzinfo=timezone.utc)
            row = {"station": cells["STN"], "lat": float(cells["LAT"]), "lon": float(cells["LON"]),
                   "observed_at": observed.isoformat(timespec="minutes")}
        except (KeyError, ValueError):
            continue
        for col, (field, cast) in COLUMNS.items():
            value = cells.get(col, "MM")
            row[field] = None if value == "MM" else cast(value)
        rows.append(row)
    return rows


def parse_stations(xml_text: str) -> dict:
    out = {}
    for st in ET.fromstring(xml_text).iter("station"):
        out[st.get("id", "").upper()] = {"name": st.get("name") or None, "owner": st.get("owner") or None,
                                         "type": st.get("type") or None}
    return out


def _max(rows, field):
    candidates = [r for r in rows if r.get(field) is not None]
    if not candidates:
        return None
    top = max(candidates, key=lambda r: r[field])
    return {"value": top[field], "station": top["station"], "name": top.get("name"), "lat": top["lat"], "lon": top["lon"]}


def build_snapshot(obs_text: str, stations: dict) -> dict:
    rows = parse_observations(obs_text)
    if not rows:
        raise RuntimeError("NDBC returned no observations")
    for r in rows:
        meta = stations.get(r["station"].upper(), {})
        r["name"], r["station_type"] = meta.get("name"), meta.get("type")
    rows.sort(key=lambda r: r["station"])
    times = sorted(r["observed_at"] for r in rows)
    waves = [r["wave_height_m"] for r in rows if r["wave_height_m"] is not None]
    return {
        "dataset": "AurAgrid Marine Weather Observations",
        "observed_between": [times[0], times[-1]],
        "stations": len(rows),
        "notes": "Latest observation per station; units: m/s, m, s, degrees true, hPa, deg C, nmi. "
                 "null = not reported by that station.",
        "summary": {
            "max_wave_height": _max(rows, "wave_height_m"),
            "max_wind_speed": _max(rows, "wind_speed_ms"),
            "max_gust": _max(rows, "gust_ms"),
            "stations_reporting_waves": len(waves),
            "stations_waves_over_4m": sum(1 for w in waves if w >= 4),
        },
        "observations": rows,
        "attribution": {
            "source": "Source: NOAA National Data Buoy Center (ndbc.noaa.gov), latest observations.",
            "terms": "NOAA data is in the U.S. public domain. NOAA does not endorse AurAgrid.",
            "transformation": "AurAgrid parses the NDBC file into JSON and adds station names and a summary.",
        },
    }


class NDBCFetcher:
    def __init__(self, session=None, clock=time.time):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.clock = clock
        self._stations, self._stations_at = {}, 0.0

    def _get(self, url: str) -> str:
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text

    def stations(self) -> dict:
        if not self._stations or self.clock() - self._stations_at > STATIONS_MAX_AGE:
            try:
                self._stations, self._stations_at = parse_stations(self._get(STATIONS_URL)), self.clock()
            except Exception:
                log.exception("NDBC station list failed; keeping the previous one")
        return self._stations

    def __call__(self) -> dict:
        return build_snapshot(self._get(OBS_URL), self.stations())


feeds.describe(
    "maritime-weather", source="NOAA National Data Buoy Center (NDBC)",
    url="https://www.ndbc.noaa.gov/data/latest_obs/latest_obs.txt",
    terms_url="https://www.weather.gov/disclaimer",
    attribution="Source: NOAA National Data Buoy Center. Public domain; NOAA does not endorse AurAgrid.",
    as_of=lambda snap: snap["observed_between"][1])


def register_from_env() -> bool:
    if os.environ.get("AURAGRID_NDBC_ENABLED", "1") != "1":
        return False
    feeds.provider("maritime-weather")(feeds.CachedFeed(
        NDBCFetcher(),
        float(os.environ.get("AURAGRID_NDBC_CACHE_SECONDS", "600")),
        float(os.environ.get("AURAGRID_NDBC_MAX_STALE_SECONDS", str(3 * 3600))),
        retry_seconds=120))
    log.info("maritime-weather provider registered (NOAA NDBC)")
    return True
