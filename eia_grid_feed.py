"""energy/grid provider: hourly U.S. electricity demand and generation mix (EIA-930).

Data: EIA Hourly Electric Grid Monitor (Form EIA-930) via API v2
`electricity/rto/region-data` (demand, day-ahead demand forecast, net
generation, interchange) and `electricity/rto/fuel-type-data` (net generation
by fuel), for the Lower 48 total, EIA's 13 regions and every balancing
authority. Hourly, typically 1-2 hours behind real time; each region is
reported at its own latest hour.

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

import requests

import eia_api
import feeds

log = logging.getLogger("auragrid.eia_grid")

HOURS_BACK = 8
RENEWABLE = {"WAT", "SUN", "WND", "GEO"}
NUCLEAR = {"NUC"}
FOSSIL = {"COL", "NG", "OIL"}
STORAGE = {"BAT", "PS", "SNB"}
US48 = "US48"
EIA_REGIONS = {"CAL", "CAR", "CENT", "FLA", "MIDA", "MIDW", "NE", "NY", "NW", "SE", "SW", "TEN", "TEX"}
REGION_TYPES = {"D": "demand_mwh", "DF": "demand_forecast_mwh", "NG": "net_generation_mwh",
                "TI": "interchange_mwh"}


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pct(part, whole):
    return round(part / whole * 100, 1) if whole else None


def build_snapshot(region_rows: list, fuel_rows: list) -> dict:
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
    return {
        "dataset": "AurAgrid U.S. Grid Demand and Generation Mix",
        "frequency": "hourly",
        "notes": "EIA-930 hourly data, usually 1-2 hours behind real time; each region at its latest "
                 "reported hour (UTC, hour ending). Shares are of positive net generation excluding storage.",
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


def fetch(session, api_key: str, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=HOURS_BACK)).strftime("%Y-%m-%dT%H")
    common = {"frequency": "hourly", "start": start,
              "sort[0][column]": "period", "sort[0][direction]": "desc"}
    region_rows = eia_api.get_rows(session, "electricity/rto/region-data", api_key,
                                   {**common, "facets[type][]": list(REGION_TYPES)})
    fuel_rows = eia_api.get_rows(session, "electricity/rto/fuel-type-data", api_key, common)
    return build_snapshot(region_rows, fuel_rows)


feeds.describe(
    "energy/grid", source="U.S. Energy Information Administration (EIA), Hourly Electric Grid Monitor (EIA-930)",
    url="https://api.eia.gov/v2/electricity/rto/", terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    attribution="Source: U.S. Energy Information Administration. Public domain; EIA does not endorse AurAgrid.",
    as_of=lambda snap: (snap.get("lower48") or {}).get("period_utc") or max(r["period_utc"] for r in snap["regions"]))


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
