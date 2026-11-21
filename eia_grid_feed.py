"""energy/grid provider: hourly U.S. electricity demand and generation mix (EIA-930).

Data: EIA Hourly Electric Grid Monitor (Form EIA-930) via API v2
`electricity/rto/region-data` (demand, day-ahead demand forecast, net
generation, interchange) and `electricity/rto/fuel-type-data` (net generation
by fuel), for the Lower 48 total, EIA's 13 regions and every balancing
authority. Hourly. Demand is usually 1-2 hours behind real time, but the
generation-by-fuel series can lag much longer (about 12 hours on 2026-10-31),
so the query window is anchored on the newest hour EIA has published for fuel
mix, not on the wall clock. Each region is reported at its own latest complete
hour.

Shares are computed from reported generation only (no emission factors):
renewable = hydro + solar + wind (+ geothermal where reported); carbon-free =
renewable + nuclear; fossil = coal + gas + oil. Battery and pumped-storage
output is listed but kept out of the shares; negative values (charging) are
ignored in totals.

    python eia_grid_feed.py --check     # one live call with EIA_API_KEY, prints a summary
"""
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import eia_api
import feeds

log = logging.getLogger("auragrid.eia_grid")

HOURS_BACK = 8   # hours before EIA's newest fuel-mix hour (slower BAs report a few hours later)
REALTIME_HOURS_BACK = 4   # hours before region-data's newest Lower-48 hour (some regions report later)
RENEWABLE = {"WAT", "SUN", "WND", "GEO"}
NUCLEAR = {"NUC"}
FOSSIL = {"COL", "NG", "OIL"}
STORAGE = {"BAT", "PS", "SNB"}
US48 = "US48"
EIA_REGIONS = {"CAL", "CAR", "CENT", "FLA", "MIDA", "MIDW", "NE", "NY", "NW", "SE", "SW", "TEN", "TEX"}
REGION_TYPES = {"D": "demand_mwh", "DF": "demand_forecast_mwh", "NG": "net_generation_mwh",
                "TI": "interchange_mwh"}
# Measured values only: on 2026-11-21 the day-ahead forecast (DF) for recent hours was reported by
# only some balancing authorities, so EIA's regional and U.S. sums were far too low.
REALTIME_TYPES = {"D": "demand_mwh", "NG": "net_generation_mwh", "TI": "interchange_mwh"}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def _hour(period: str) -> datetime:
    return datetime.strptime(period, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)


def _reporting(by: dict, period: str, code: str) -> int:
    """How many of EIA's 13 regions reported `code` for `period`."""
    return sum(1 for resp in EIA_REGIONS if code in by.get(resp, {}).get(period, {}))


def build_realtime(rows: list) -> Optional[dict]:
    """Newest complete hour of measured demand / net generation / interchange (no forecasts).

    EIA publishes these within about an hour (region-data), while the fuel mix can lag a day
    or more, so they are reported separately, each region at its own newest hour of actual
    demand. Net generation and interchange are filled only when EIA has them for that same
    hour (on 2026-11-21 they lagged with the fuel mix); otherwise they are null, never
    taken from an older hour."""
    by = {}
    names = {}
    for r in rows:
        value = _num(r.get("value"))
        if value is None or r.get("type") not in REALTIME_TYPES:
            continue
        resp = r["respondent"]
        if resp != US48 and resp not in EIA_REGIONS:
            continue
        names[resp] = r.get("respondent-name") or names.get(resp)
        by.setdefault(resp, {}).setdefault(r["period"], {})[r["type"]] = value
    out = []
    for resp, periods in by.items():
        ready = [p for p, v in periods.items() if "D" in v]   # actual demand (DF alone is a forecast)
        if resp == US48:
            # EIA's Lower-48 total is the sum of the regions that have reported: only an hour
            # with all 13 regions' actual demand is a complete total.
            ready = [p for p in ready if _reporting(by, p, "D") == len(EIA_REGIONS)]
        if not ready:
            continue
        period = max(ready)
        stats = periods[period]
        row = {"respondent": resp, "name": names.get(resp),
               "kind": "lower48" if resp == US48 else "eia_region", "period_utc": period,
               **{field: stats.get(code) for code, field in REALTIME_TYPES.items()}}
        if resp == US48:
            partial = {}
            for code, field in REALTIME_TYPES.items():
                n = _reporting(by, period, code)
                if row[field] is not None and n < len(EIA_REGIONS):
                    row[field] = None   # a partial sum would understate the U.S. total
                    partial[field] = "%d of %d regions reported this hour" % (n, len(EIA_REGIONS))
            if partial:
                row["incomplete"] = partial
        out.append(row)
    lower48 = next((r for r in out if r["kind"] == "lower48"), None)
    if lower48 is None:
        return None
    out.sort(key=lambda r: (r["kind"] != "lower48", r["respondent"]))
    return {"period_utc": lower48["period_utc"], "lower48": lower48,
            "regions": [r for r in out if r["kind"] != "lower48"]}


def build_snapshot(region_rows: list, fuel_rows: list, realtime_rows: Optional[list] = None) -> dict:
    # respondent -> period -> {fuel: mwh}
    by_fuel, names = {}, {}
    for r in fuel_rows:
        value = _num(r.get("value"))
        if value is None:
            continue
        resp = r["respondent"]
        names[resp] = r.get("respondent-name") or names.get(resp)
        by_fuel.setdefault(resp, {}).setdefault(r["period"], {})[r["fueltype"]] = value
    by_type = {}
    for r in region_rows:
        value = _num(r.get("value"))
        if value is None:
            continue
        resp = r["respondent"]
        names[resp] = r.get("respondent-name") or names.get(resp)
        by_type.setdefault(resp, {}).setdefault(r["period"], {})[r["type"]] = value

    regions = []
    for resp, periods in by_fuel.items():
        # latest hour with every fuel this respondent reports (the newest hour can be partial)
        full = max(len(mix) for mix in periods.values())
        period = max(p for p, mix in periods.items() if len(mix) == full)
        mix = periods[period]
        generation = sum(v for f, v in mix.items() if f not in STORAGE and v > 0)
        if generation <= 0:
            continue
        share = lambda codes: _pct(sum(max(mix.get(f, 0), 0) for f in codes), generation)  # noqa: E731
        stats = by_type.get(resp, {}).get(period, {})
        regions.append({
            "respondent": resp, "name": names.get(resp),
            "kind": "lower48" if resp == US48 else ("eia_region" if resp in EIA_REGIONS else "balancing_authority"),
            "period_utc": period,
            **{field: stats.get(code) for code, field in REGION_TYPES.items()},
            "generation_by_fuel_mwh": dict(sorted(mix.items())),
            "renewable_share_pct": share(RENEWABLE),
            "carbon_free_share_pct": share(RENEWABLE | NUCLEAR),
            "fossil_share_pct": share(FOSSIL),
        })
    if not regions:
        raise RuntimeError("EIA-930 returned no generation data")
    regions.sort(key=lambda r: (r["kind"] != "lower48", r["kind"] != "eia_region", r["respondent"]))
    lower48 = next((r for r in regions if r["kind"] == "lower48"), None)
    bas = [r for r in regions if r["kind"] == "balancing_authority" and sum(
        v for f, v in r["generation_by_fuel_mwh"].items() if f not in STORAGE and v > 0) >= 500]
    cleanest = sorted(bas, key=lambda r: -(r["carbon_free_share_pct"] or 0))[:10]
    realtime = build_realtime(realtime_rows or [])
    mix_period = (lower48 or {}).get("period_utc")
    lag = None
    if realtime and mix_period:
        lag = round((_hour(realtime["period_utc"]) - _hour(mix_period)).total_seconds() / 3600)
    return {
        "dataset": "AurAgrid U.S. Grid Demand and Generation Mix",
        "frequency": "hourly",
        "notes": "EIA-930 hourly data, all hours UTC, hour ending. 'realtime' is the newest hour of measured demand, "
                 "net generation and interchange (demand usually within about an hour; a U.S. total only when all 13 "
                 "regions reported, otherwise null with the reason in 'incomplete'). The "
                 "generation mix ('lower48', 'regions', shares) is EIA's newest fuel-type hour, which lags real time "
                 "by 12 hours or more: see generation_mix_period_utc and generation_mix_lag_hours. Shares are of "
                 "positive net generation excluding storage.",
        "realtime": realtime,
        "generation_mix_period_utc": mix_period,
        "generation_mix_lag_hours": lag,
        "lower48": lower48,
        "cleanest_balancing_authorities": [
            {"respondent": r["respondent"], "name": r["name"], "carbon_free_share_pct": r["carbon_free_share_pct"],
             "renewable_share_pct": r["renewable_share_pct"], "period_utc": r["period_utc"]} for r in cleanest],
        "regions": regions,
        "attribution": {
            "source": "Source: U.S. Energy Information Administration (EIA), Hourly Electric Grid Monitor "
                      "(Form EIA-930), accessed %s." % datetime.now(timezone.utc).date().isoformat(),
            "terms": "EIA data is in the U.S. public domain. EIA does not endorse AurAgrid.",
            "transformation": "AurAgrid selects each region's latest hour and computes generation shares; "
                              "EIA values are unaltered.",
        },
    }


def fetch(session, api_key: str) -> dict:
    newest = datetime.strptime(eia_api.latest_period(session, "electricity/rto/fuel-type-data", api_key,
                                                     {"frequency": "hourly"}), "%Y-%m-%dT%H")
    start = (newest - timedelta(hours=HOURS_BACK)).strftime("%Y-%m-%dT%H")
    common = {"frequency": "hourly", "start": start,
              "sort[0][column]": "period", "sort[0][direction]": "desc"}
    region_rows = eia_api.get_rows(session, "electricity/rto/region-data", api_key,
                                   {**common, "facets[type][]": list(REGION_TYPES)})
    fuel_rows = eia_api.get_rows(session, "electricity/rto/fuel-type-data", api_key, common)
    # Current hours of demand / net generation: anchored on the newest hour of actual Lower-48 demand.
    newest_rt = _hour(eia_api.latest_period(session, "electricity/rto/region-data", api_key,
                                            {"frequency": "hourly", "facets[respondent][]": US48,
                                             "facets[type][]": "D"}))   # actual demand: DF runs into the future
    realtime_rows = eia_api.get_rows(session, "electricity/rto/region-data", api_key, {
        "frequency": "hourly", "start": (newest_rt - timedelta(hours=REALTIME_HOURS_BACK)).strftime("%Y-%m-%dT%H"),
        "sort[0][column]": "period", "sort[0][direction]": "desc",
        "facets[type][]": list(REALTIME_TYPES), "facets[respondent][]": sorted(EIA_REGIONS | {US48})})
    return build_snapshot(region_rows, fuel_rows, realtime_rows)


feeds.describe(
    "energy/grid", source="U.S. Energy Information Administration (EIA), Hourly Electric Grid Monitor (EIA-930)",
    url="https://api.eia.gov/v2/electricity/rto/", terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    attribution="Source: U.S. Energy Information Administration. Public domain; EIA does not endorse AurAgrid.",
    license='U.S. government work, public domain; credit the U.S. Energy Information Administration',
    # The feed is as fresh as its newest hour (realtime demand); the mix carries its own hour and lag.
    as_of=lambda snap: ((snap.get("realtime") or {}).get("period_utc")
                        or (snap.get("lower48") or {}).get("period_utc")
                        or max(r["period_utc"] for r in snap["regions"])))


def register_from_env() -> bool:
    key = os.environ.get("EIA_API_KEY")
    if not key:
        log.warning("EIA_API_KEY not set: energy/grid stays 'Feed not connected'")
        return False
    session = requests.Session()
    session.headers["User-Agent"] = "AurAgrid-EIA/1.0"
    feeds.provider("energy/grid")(feeds.CachedFeed(
        lambda: fetch(session, key),
        float(os.environ.get("AURAGRID_EIA_GRID_CACHE_SECONDS", "900")),
        float(os.environ.get("AURAGRID_EIA_GRID_MAX_STALE_SECONDS", str(6 * 3600))),
        retry_seconds=300))
    log.info("energy/grid provider registered (EIA-930)")
    return True


if __name__ == "__main__":
    import json
    import sys
    if "--check" not in sys.argv or not os.environ.get("EIA_API_KEY"):
        sys.exit("usage: EIA_API_KEY=... python eia_grid_feed.py --check")
    snap = fetch(requests.Session(), os.environ["EIA_API_KEY"])
    print(json.dumps({"lower48": snap["lower48"], "regions": len(snap["regions"]),
                      "cleanest": snap["cleanest_balancing_authorities"][:3]}, indent=1))
