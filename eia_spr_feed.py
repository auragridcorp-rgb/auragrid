"""spr-stockpile provider: U.S. Strategic Petroleum Reserve and crude stocks, weekly (EIA).

Data: EIA Weekly Petroleum Status Report stocks via API v2
`petroleum/stoc/wstk`: series WCSSTUS1 (crude oil in the SPR), WCESTUS1
(commercial crude, excluding SPR) and WCRSTUS1 (total crude including SPR),
in thousand barrels, weekly (published Wednesdays for the week ending the
previous Friday).

Each snapshot: latest SPR level with 1-, 4- and 52-week changes and the
52-week range, the same for commercial crude, and a 52-week history.

    python eia_spr_feed.py --check     # one live call with EIA_API_KEY, prints a summary
"""
import logging
import os
from datetime import datetime, timedelta, timezone

import requests

import eia_api
import feeds

log = logging.getLogger("auragrid.eia_spr")

SERIES = {"WCSSTUS1": "spr", "WCESTUS1": "commercial", "WCRSTUS1": "total_incl_spr"}
WEEKS = 60


def _series_summary(points: list) -> dict:
    """points: [(period, thousand_bbl)] newest first."""
    latest_period, latest = points[0]
    at = lambda n: points[n][1] if len(points) > n else None  # noqa: E731
    year = [v for _, v in points[:52]]

    def change(n):
        past = at(n)
        return None if past is None else round((latest - past) / 1000, 3)

    return {
        "week_ending": latest_period,
        "million_barrels": round(latest / 1000, 3),
        "change_1w_million_barrels": change(1),
        "change_4w_million_barrels": change(4),
        "change_52w_million_barrels": change(52),
        "range_52w_million_barrels": [round(min(year) / 1000, 3), round(max(year) / 1000, 3)],
    }


def build_snapshot(rows: list) -> dict:
    series = {}
    for r in rows:
        name = SERIES.get(r.get("series"))
        try:
            value = float(r.get("value"))
        except (TypeError, ValueError):
            continue
        if name:
            series.setdefault(name, {})[r["period"]] = value
    if "spr" not in series:
        raise RuntimeError("EIA returned no SPR stock data")
    ordered = {name: sorted(values.items(), reverse=True) for name, values in series.items()}
    return {
        "dataset": "AurAgrid U.S. Strategic Petroleum Reserve Monitor",
        "frequency": "weekly",
        "notes": "EIA weekly stocks, published Wednesdays for the week ending the previous Friday. "
                 "Million barrels; changes are versus 1, 4 and 52 weeks earlier.",
        "spr": _series_summary(ordered["spr"]),
        "commercial_crude": _series_summary(ordered["commercial"]) if ordered.get("commercial") else None,
        "total_crude_incl_spr": _series_summary(ordered["total_incl_spr"]) if ordered.get("total_incl_spr") else None,
        "history": [{"week_ending": p, "spr_million_barrels": round(v / 1000, 3),
                     "commercial_million_barrels": round(series.get("commercial", {})[p] / 1000, 3)
                     if p in series.get("commercial", {}) else None}
                    for p, v in ordered["spr"][:52]],
        "attribution": {
            "source": "Source: U.S. Energy Information Administration (EIA), Weekly Petroleum Status Report "
                      "(week ending %s)." % ordered["spr"][0][0],
            "terms": "EIA data is in the U.S. public domain. EIA does not endorse AurAgrid.",
            "transformation": "AurAgrid converts thousand barrels to million barrels and computes changes "
                              "and ranges; EIA values are unaltered.",
        },
    }


def fetch(session, api_key: str, today=None) -> dict:
    today = today or datetime.now(timezone.utc).date()
    start = (today - timedelta(weeks=WEEKS)).isoformat()
    rows = eia_api.get_rows(session, "petroleum/stoc/wstk", api_key, {
        "frequency": "weekly", "start": start, "facets[series][]": list(SERIES),
        "sort[0][column]": "period", "sort[0][direction]": "desc"})
    return build_snapshot(rows)


feeds.describe(
    "spr-stockpile", source="U.S. Energy Information Administration (EIA), Weekly Petroleum Status Report",
    url="https://api.eia.gov/v2/petroleum/stoc/wstk/", terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    attribution="Source: U.S. Energy Information Administration. Public domain; EIA does not endorse AurAgrid.",
    license='U.S. government work, public domain; credit the U.S. Energy Information Administration',
    as_of=lambda snap: snap["spr"]["week_ending"])


def register_from_env() -> bool:
    key = os.environ.get("EIA_API_KEY")
    if not key:
        log.warning("EIA_API_KEY not set: spr-stockpile stays 'Feed not connected'")
        return False
    session = requests.Session()
    session.headers["User-Agent"] = "AurAgrid-EIA/1.0"
    feeds.provider("spr-stockpile")(feeds.CachedFeed(
        lambda: fetch(session, key),
        float(os.environ.get("AURAGRID_EIA_SPR_CACHE_SECONDS", str(6 * 3600))),
        float(os.environ.get("AURAGRID_EIA_SPR_MAX_STALE_SECONDS", str(14 * 86400))),
        retry_seconds=900))
    log.info("spr-stockpile provider registered (EIA weekly stocks)")
    return True


if __name__ == "__main__":
    import json
    import sys
    if "--check" not in sys.argv or not os.environ.get("EIA_API_KEY"):
        sys.exit("usage: EIA_API_KEY=... python eia_spr_feed.py --check")
    snap = fetch(requests.Session(), os.environ["EIA_API_KEY"])
    print(json.dumps({k: snap[k] for k in ("spr", "commercial_crude", "total_crude_incl_spr")}, indent=1))
