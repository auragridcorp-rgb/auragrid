"""Derived endpoints: computed only from feeds AurAgrid already stores (machine-discovery Phase 4).

Every result is labelled method="derived" with its formula in provenance.derivation and
the as_of / staleness of each input. Nothing here forecasts: these are descriptive
statistics of past observations and of measured values.

    fee_timing(store)                  Bitcoin fee percentiles, trend and lowest windows
    area_risk(store, lat, lon, km)     earthquake counts and marine measurements near a point
    energy_snapshot(store)             EIA grid, SPR and LNG summaries in one document
    grid_carbon(store)                 gCO2/kWh from the EIA-930 generation mix
"""
import math
import statistics
import time
from datetime import datetime, timezone
from typing import Optional

import jobs

HOUR, DAY = 3600, 86400


class InputUnavailable(Exception):
    """An input feed has no data, or its newest data is beyond its max age (answer 503)."""

    def __init__(self, feed: str, as_of: Optional[str] = None):
        super().__init__(feed)
        self.feed, self.as_of = feed, as_of


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _input(store, feed: str, clock=time.time):
    """(retrieved_at, data, info) for a feed's newest snapshot; raises when unusable."""
    snap = store.latest(feed)
    if snap is None:
        raise InputUnavailable(feed)
    stale_after, max_age = jobs.LIMITS[feed]
    age = clock() - snap.retrieved_at
    if age > max_age:
        raise InputUnavailable(feed, snap.as_of)
    info = {"feed": feed, "as_of": snap.as_of, "retrieved_at": _iso(snap.retrieved_at),
            "age_seconds": round(age), "stale_after_seconds": stale_after, "stale": age > stale_after}
    return snap.retrieved_at, snap.data, info


def _pct_rank(values, x) -> float:
    return round(100.0 * sum(1 for v in values if v <= x) / len(values), 1)


def _quantile(sorted_values, q):
    if not sorted_values:
        return None
    i = (len(sorted_values) - 1) * q
    lo, hi = math.floor(i), math.ceil(i)
    return round(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (i - lo), 3)


# ---------------------------------------------------------------------------
# 1. Fee-timing signal (btc-fees history)
# ---------------------------------------------------------------------------
FEE_TARGET = "6"   # blocks: the primary series; 1 and 144 are reported alongside


def _fee(snap: dict, target: str) -> Optional[float]:
    est = (snap.get("fee_estimates") or {}).get(target) or {}
    return est.get("sat_per_vb")


def fee_timing(store, clock=time.time) -> dict:
    now = clock()
    _, latest, info = _input(store, "btc-fees", clock)
    history = [(ts, d) for ts, d in store.history("btc-fees", now - 7 * DAY) if _fee(d, FEE_TARGET) is not None]
    if not history:
        raise InputUnavailable("btc-fees", info["as_of"])
    current = _fee(latest, FEE_TARGET)

    def window(seconds):
        pts = [(ts, _fee(d, FEE_TARGET)) for ts, d in history if ts >= now - seconds]
        values = sorted(v for _, v in pts)
        if not values:
            return None
        low_ts, low_v = min(pts, key=lambda p: (p[1], -p[0]))
        return {"observations": len(values), "min": values[0], "p10": _quantile(values, 0.1),
                "median": _quantile(values, 0.5), "p90": _quantile(values, 0.9), "max": values[-1],
                "current_percentile_rank": _pct_rank(values, current) if current is not None else None,
                "lowest": {"sat_per_vb": low_v, "last_observed_at": _iso(low_ts)}}

    # Trend: least-squares slope of the 6-block rate over the last 6 hours.
    recent = [(ts, v) for ts, v in ((ts, _fee(d, FEE_TARGET)) for ts, d in history) if ts >= now - 6 * HOUR]
    trend = None
    if len(recent) >= 10:
        xs = [(ts - now) / HOUR for ts, _ in recent]
        ys = [v for _, v in recent]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx else 0.0
        change = slope * 6
        direction = "flat" if my == 0 or abs(change) / my < 0.05 else ("rising" if slope > 0 else "falling")
        trend = {"window_hours": 6, "slope_sat_per_vb_per_hour": round(slope, 4), "direction": direction,
                 "observations": len(recent)}

    # Hours of the UTC day with the lowest median 6-block rate over the past 7 days.
    by_hour = {}
    for ts, d in history:
        by_hour.setdefault(datetime.fromtimestamp(ts, timezone.utc).hour, []).append(_fee(d, FEE_TARGET))
    hours = sorted(({"hour_utc": h, "median_sat_per_vb": round(statistics.median(v), 3), "observations": len(v)}
                    for h, v in by_hour.items() if len(v) >= 5), key=lambda r: (r["median_sat_per_vb"], r["hour_utc"]))

    span_days = round((now - history[0][0]) / DAY, 2)
    data = {
        "target_blocks": int(FEE_TARGET),
        "current": {"observed_at": info["as_of"],
                    "sat_per_vb": {t: _fee(latest, t) for t in ("1", "6", "144")},
                    "mempool_min_fee_sat_per_vb": (latest.get("mempool") or {}).get("min_fee_sat_per_vb")},
        "last_24h": window(DAY),
        "last_7d": window(7 * DAY),
        "history_span_days": span_days,
        "trend": trend,
        "lowest_median_hours_utc_7d": hours[:3],
        "caveat": "Descriptive statistics of past estimates from AurAgrid's own node; not a forecast.",
    }
    derivation = ("Inputs: every stored btc-fees snapshot of the past 7 days (one per minute since 2026-11-11). "
                  "Series: Bitcoin Core estimatesmartfee (economical) for a 6-block target, sat/vB. "
                  "percentile_rank = share of observations in the window <= the current value. "
                  "trend = least-squares slope over the last 6 hours; 'flat' when the 6-hour change is under 5% "
                  "of the mean. lowest_median_hours = UTC hours ranked by the median 6-block rate over 7 days.")
    return _result("derived/fee-timing", data, [info], derivation,
                   source="Derived by AurAgrid from its own Bitcoin Core node's fee estimates")


# ---------------------------------------------------------------------------
# 2. Seismic and marine flags near a point (earthquakes + maritime-weather)
# ---------------------------------------------------------------------------
GALE_GUST_MS = 17.2      # Beaufort 8 lower bound
STORM_WIND_MS = 24.5     # Beaufort 10 lower bound
HIGH_WAVES_M = 4.0


def _km(lat1, lon1, lat2, lon2) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def area_risk(store, lat: float, lon: float, radius_km: float, clock=time.time) -> dict:
    if not (-90 <= lat <= 90 and -180 <= lon <= 180 and 10 <= radius_km <= 2000):
        raise ValueError("lat in [-90, 90], lon in [-180, 180], radius_km in [10, 2000]")
    _, quakes, q_info = _input(store, "earthquakes", clock)
    _, marine, m_info = _input(store, "maritime-weather", clock)
    events = quakes.get("events")
    if events is None:   # snapshots stored before the event list existed
        raise InputUnavailable("earthquakes", q_info["as_of"])

    def near(evs):
        out = []
        for e in evs:
            d = _km(lat, lon, e["lat"], e["lon"])
            if d <= radius_km:
                out.append({**e, "distance_km": round(d, 1)})
        return sorted(out, key=lambda e: -(e["magnitude"] or 0))

    q24, q7 = near(events.get("m2_5_plus_24h", [])), near(events.get("m4_5_plus_7d", []))
    stations = []
    for o in marine.get("observations", []):
        if o.get("lat") is None or o.get("lon") is None:
            continue
        d = _km(lat, lon, o["lat"], o["lon"])
        if d <= radius_km:
            stations.append({**o, "distance_km": round(d, 1)})

    def peak(field):
        vals = [s for s in stations if s.get(field) is not None]
        if not vals:
            return None
        s = max(vals, key=lambda s: s[field])
        return {"value": s[field], "station": s["station"], "distance_km": s["distance_km"],
                "observed_at": s.get("observed_at")}

    low_p = [s for s in stations if s.get("pressure_hpa") is not None]
    marine_out = {"stations_in_radius": len(stations), "max_wave_height_m": peak("wave_height_m"),
                  "max_wind_speed_ms": peak("wind_speed_ms"), "max_gust_ms": peak("gust_ms"),
                  "min_pressure_hpa": ({"value": min(low_p, key=lambda s: s["pressure_hpa"])["pressure_hpa"]}
                                       if low_p else None)}
    flags = {
        "earthquake_m5_plus_7d": any((e["magnitude"] or 0) >= 5 for e in q7),
        "earthquake_tsunami_flag_7d": any(e.get("tsunami_flag") for e in q7),
        "waves_over_4m": any((s.get("wave_height_m") or 0) >= HIGH_WAVES_M for s in stations),
        "gale_force_gust": any((s.get("gust_ms") or 0) >= GALE_GUST_MS for s in stations),
        "storm_force_wind": any((s.get("wind_speed_ms") or 0) >= STORM_WIND_MS for s in stations),
    }
    data = {
        "area": {"lat": lat, "lon": lon, "radius_km": radius_km},
        "flags": flags,
        "earthquakes": {"m2_5_plus_24h": {"count": len(q24), "largest": q24[:5]},
                        "m4_5_plus_7d": {"count": len(q7), "largest": q7[:5]}},
        "marine": marine_out,
        "thresholds": {"waves_over_4m": "wave height >= 4.0 m",
                       "gale_force_gust": "gust >= 17.2 m/s (Beaufort 8)",
                       "storm_force_wind": "sustained wind >= 24.5 m/s (Beaufort 10)",
                       "earthquake_m5_plus_7d": "any M5.0+ event within the radius in the past 7 days"},
        "caveat": "Counts and measured values only; no forecasts. USGS magnitudes and locations can be revised. "
                  "Areas without NDBC stations report no marine data, not calm seas.",
    }
    derivation = ("Great-circle (haversine) distance from the point to each USGS event (M2.5+ past 24 h, M4.5+ "
                  "past 7 days) and each NOAA NDBC station's latest observation; counts and maxima within "
                  "radius_km; flags are fixed thresholds on measured values (see data.thresholds).")
    return _result("derived/area-risk", data, [q_info, m_info], derivation,
                   source="Derived by AurAgrid from USGS earthquake and NOAA NDBC observation data",
                   credit="Credit: U.S. Geological Survey; NOAA National Data Buoy Center. No endorsement by "
                          "USGS or NOAA is implied.")


# ---------------------------------------------------------------------------
# 3. Grid and energy snapshot (energy/grid + spr-stockpile + lng-supply-chain)
# ---------------------------------------------------------------------------
def energy_snapshot(store, clock=time.time) -> dict:
    _, grid, g_info = _input(store, "energy/grid", clock)
    _, spr, s_info = _input(store, "spr-stockpile", clock)
    _, lng, l_info = _input(store, "lng-supply-chain", clock)
    l48 = grid.get("lower48") or {}
    data = {
        "electricity_lower48": {k: l48.get(k) for k in ("period_utc", "demand_mwh", "demand_forecast_mwh",
                                                        "net_generation_mwh", "renewable_share_pct",
                                                        "carbon_free_share_pct", "fossil_share_pct")},
        "crude_stocks": {k: spr.get(k) for k in ("spr", "commercial_crude", "total_crude_incl_spr")},
        "lng_exports": {"period": lng.get("period"), **(lng.get("us_total") or {})},
        "parts": {"electricity_lower48": g_info, "crude_stocks": s_info, "lng_exports": l_info},
    }
    derivation = ("No new numbers: the Lower-48 summary of energy/grid, the SPR/commercial/total crude summaries "
                  "of spr-stockpile and the U.S. totals of lng-supply-chain, side by side, each with its own as_of "
                  "and stale flag (data.parts).")
    return _result("derived/energy-snapshot", data, [g_info, s_info, l_info], derivation,
                   source="Combined by AurAgrid from U.S. Energy Information Administration data",
                   credit=EIA_CREDIT)


# ---------------------------------------------------------------------------
# 4. Grid carbon intensity (energy/grid generation mix x EIA emission rates)
# ---------------------------------------------------------------------------
LB_TO_G = 453.59237
EIA_CO2_FAQ = "https://www.eia.gov/tools/faqs/faq.php?id=74&t=11"
# EIA FAQ (2023 data, utility-scale plants), pounds of CO2 per kWh generated.
CO2_LB_PER_KWH = {"COL": 2.31, "NG": 0.96, "OIL": 2.46}
ZERO_DIRECT = {"NUC", "SUN", "WND", "WAT", "GEO"}      # EIA treats these as zero direct CO2
EXCLUDED = {"BAT", "PS", "OTH", "UNK", "OES", "UES", "SNB"}   # storage, unclassified or mixed
EIA_CREDIT = ("Source data: U.S. Energy Information Administration. Values derived by AurAgrid; "
              "EIA does not endorse AurAgrid.")


def _intensity(mix: dict) -> dict:
    counted = {f: v for f, v in (mix or {}).items() if f not in EXCLUDED and v is not None and v > 0}
    total = sum(counted.values())
    if total <= 0:
        return {"g_co2_per_kwh": None}
    grams = sum(v * CO2_LB_PER_KWH.get(f, 0.0) * LB_TO_G for f, v in counted.items())
    all_pos = sum(v for v in (mix or {}).values() if v is not None and v > 0)
    unknown = sorted(f for f in counted if f not in CO2_LB_PER_KWH and f not in ZERO_DIRECT)
    return {"g_co2_per_kwh": round(grams / total, 1),
            "generation_counted_mwh": round(total, 1),
            "excluded_share_pct": round(100 * (all_pos - total) / all_pos, 1) if all_pos else None,
            **({"fuels_assumed_zero_co2": unknown} if unknown else {})}


def grid_carbon(store, clock=time.time) -> dict:
    _, grid, g_info = _input(store, "energy/grid", clock)
    l48 = grid.get("lower48") or {}
    regions = []
    for r in grid.get("regions", []):
        regions.append({"respondent": r.get("respondent"), "name": r.get("name"), "kind": r.get("kind"),
                        "period_utc": r.get("period_utc"), **_intensity(r.get("generation_by_fuel_mwh"))})
    regions.sort(key=lambda r: (r["g_co2_per_kwh"] is None, r["g_co2_per_kwh"] or 0))
    data = {
        "lower48": {"period_utc": l48.get("period_utc"), **_intensity(l48.get("generation_by_fuel_mwh"))},
        "regions": regions,
        "emission_rates_g_co2_per_kwh": {f: round(v * LB_TO_G, 1) for f, v in CO2_LB_PER_KWH.items()},
        "caveat": "Direct (smokestack) CO2 from U.S.-average 2023 per-fuel rates, not plant- or region-specific "
                  "rates and not lifecycle emissions. Imports, storage and unclassified generation are excluded.",
    }
    derivation = ("g_co2_per_kwh = sum over fuels of (EIA-930 net generation by fuel, MWh x emission rate) / "
                  "sum of counted generation. Rates: EIA, 'How much carbon dioxide is produced per kilowatthour of "
                  "U.S. electricity generation?' (2023 data, utility-scale plants): coal 2.31, natural gas 0.96, "
                  "petroleum 2.46 lb CO2/kWh; nuclear, solar, wind, hydro and geothermal 0. Battery, pumped "
                  "storage and other/unknown categories are excluded from both sums (excluded_share_pct).")
    result = _result("derived/grid-carbon", data, [g_info], derivation,
                     source="Derived by AurAgrid from U.S. Energy Information Administration data",
                     credit=EIA_CREDIT)
    result["provenance"]["emission_factors"] = {"title": "EIA FAQ: CO2 per kWh of U.S. electricity generation "
                                                         "(2023 data)", "url": EIA_CO2_FAQ,
                                                "license": "U.S. government work, public domain; credit EIA"}
    return result


def _result(feed: str, data: dict, inputs: list, derivation: str, source: str, credit: Optional[str] = None) -> dict:
    newest = max(inputs, key=lambda i: i["retrieved_at"])
    prov = {"source": source, "method": "derived", "derivation": derivation, "inputs": inputs,
            "as_of": min(i["as_of"] for i in inputs if i["as_of"]) if any(i["as_of"] for i in inputs) else None,
            "retrieved_at": newest["retrieved_at"], "terms_url": "/terms"}
    if credit:
        prov["attribution"] = credit
    return {"feed": feed, "method": "derived", "stale": any(i["stale"] for i in inputs), "data": data,
            "provenance": prov}


# ---------------------------------------------------------------------------
# Catalog metadata (read by app/main.py and gen_aperture.py)
# ---------------------------------------------------------------------------
# Free previews: each carries a proposed price for the owner to approve before anything is sold.
# EIA-based ones stay free until the owner decides (EIA's API terms do not address commercial use).
ENDPOINTS = {
    "derived/fee-timing": {
        "name": "Bitcoin Fee-Timing Signal", "func": fee_timing, "inputs": ["btc-fees"], "tier": "bitcoin",
        "description": "Where the current 6-block fee sits in its 24-hour and 7-day distribution, the 6-hour trend, "
                       "the lowest fees observed and the UTC hours with the lowest median fee over 7 days. "
                       "Descriptive statistics from AurAgrid's own node, not a forecast.",
        "license": "AurAgrid's own computation; use under AurAgrid's /terms",
        "proposed_price_usd": 0.02},
    "derived/area-risk": {
        "name": "Seismic and Marine Flags Near a Point", "func": area_risk,
        "inputs": ["earthquakes", "maritime-weather"], "tier": "hazards",
        "description": "Earthquake counts (M2.5+ 24 h, M4.5+ 7 days) and the highest measured waves, wind and gusts "
                       "from NOAA buoys within radius_km of lat/lon, with threshold flags. Measured values only.",
        "license": "Derived from U.S. public-domain data (USGS, NOAA); credit both, no implied endorsement",
        "proposed_price_usd": 0.02,
        "parameters": {"lat": "latitude, -90..90", "lon": "longitude, -180..180",
                       "radius_km": "10..2000 (default 500)"}},
    "derived/energy-snapshot": {
        "name": "U.S. Energy Snapshot", "func": energy_snapshot,
        "inputs": ["energy/grid", "spr-stockpile", "lng-supply-chain"], "tier": "energy",
        "description": "Lower-48 electricity demand and generation shares, SPR and commercial crude stocks, and "
                       "U.S. LNG exports in one document, each part with its own as_of and stale flag.",
        "license": "Derived from EIA data (public domain); credit EIA, no implied endorsement",
        "proposed_price_usd": 0.02, "terms_check": "EIA API terms do not address commercial use: free until decided"},
    "derived/grid-carbon": {
        "name": "U.S. Grid Carbon Intensity", "func": grid_carbon, "inputs": ["energy/grid"], "tier": "energy",
        "description": "Direct CO2 intensity (g/kWh) of the Lower 48 and every EIA-930 region and balancing "
                       "authority, from the hourly generation mix and EIA's 2023 per-fuel emission rates.",
        "license": "Derived from EIA data (public domain); credit EIA, no implied endorsement",
        "proposed_price_usd": 0.03, "terms_check": "EIA API terms do not address commercial use: free until decided"},
}
