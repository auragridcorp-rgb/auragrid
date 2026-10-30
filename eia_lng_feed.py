"""lng-supply-chain provider: U.S. LNG exports from the U.S. EIA Open Data API.

Data: EIA "U.S. Natural Gas Exports and Re-Exports by Point of Exit" (API v2
route natural-gas/move/poe2), process ENG (LNG export volumes, MMcf) and PNG
(LNG export prices, $/Mcf). Monthly, published with a lag of about three
months: this is a monthly U.S. LNG export monitor, not vessel tracking.

Each snapshot covers the latest month: U.S. total (MMcf and Bcf/d) with
month-on-month and year-on-year changes, every export terminal, top
destination countries, average export prices, and a 24-month U.S. series.

Terms: EIA data is U.S. public domain; attribution to the U.S. Energy
Information Administration is expected, and EIA must not be presented as
endorsing this product. Get a free key at https://www.eia.gov/opendata/register.php.

Environment
-----------
EIA_API_KEY                    required; the provider is only registered when set
AURAGRID_EIA_CACHE_SECONDS     default 12h (the data changes monthly)
AURAGRID_EIA_MAX_STALE_SECONDS default 45 days
"""
import calendar
import logging
import os
import re
import time
from datetime import date

import requests

import feeds

log = logging.getLogger("auragrid.eia")

URL = "https://api.eia.gov/v2/natural-gas/move/poe2/data/"
MONTHS = 25          # latest month + 24 months of history (for year-on-year)
PAGE = 5000          # EIA's maximum rows per request
SOURCE = "Source: U.S. Energy Information Administration (EIA), U.S. Natural Gas Exports and Re-Exports by Point of Exit"


def _shift(period: str, months: int) -> str:
    y, m = map(int, period.split("-"))
    total = y * 12 + (m - 1) + months
    return "%04d-%02d" % (total // 12, total % 12 + 1)


def _bcf_per_day(mmcf: float, period: str) -> float:
    y, m = map(int, period.split("-"))
    return round(mmcf / 1000 / calendar.monthrange(y, m)[1], 3)


def _pct(new, old):
    return round((new / old - 1) * 100, 1) if new is not None and old else None


def _terminal_name(description: str) -> str:
    return description.split(" Liquefied")[0].strip()


def _country_name(description: str) -> str:
    m = re.search(r" to (.+?)(?: \(|$)", description)
    return m.group(1).strip() if m else description


def fetch_rows(session, api_key: str, start: str, attempts: int = 3, sleep=time.sleep) -> list:
    rows, offset = [], 0
    while True:
        params = {"api_key": api_key, "frequency": "monthly", "data[0]": "value",
                  "facets[process][]": ["ENG", "PNG"], "start": start,
                  "sort[0][column]": "period", "sort[0][direction]": "desc",
                  "offset": offset, "length": PAGE}
        for attempt in range(attempts):
            try:
                resp = session.get(URL, params=params, timeout=30)
                body = resp.json()
                if "error" in body:
                    raise RuntimeError("EIA error %s" % body["error"].get("code", body["error"]))
                resp.raise_for_status()
                break
            except Exception as exc:
                if attempt + 1 == attempts:
                    raise RuntimeError("EIA request failed: %s" % exc)
                sleep(2 * (attempt + 1))
        page = body["response"]["data"]
        rows += page
        if len(page) < PAGE or len(rows) >= int(body["response"].get("total") or 0):
            return rows
        offset += PAGE


def build_snapshot(rows: list) -> dict:
    vol, price, desc = {}, {}, {}
    for r in rows:
        if r.get("value") in (None, ""):
            continue
        key = (r["duoarea"], r["period"])
        (vol if r["process"] == "ENG" else price)[key] = float(r["value"])
        if r["process"] == "ENG":
            desc[r["duoarea"]] = (r.get("series-description") or "", r.get("area-name"))

    us_periods = sorted(p for (a, p) in vol if a == "NUS-Z00")
    if not us_periods:
        raise RuntimeError("EIA returned no U.S. LNG export totals")
    last = us_periods[-1]
    prev, year_ago = _shift(last, -1), _shift(last, -12)
    us = vol[("NUS-Z00", last)]

    terminals = []
    for (area, period), mmcf in vol.items():
        if period == last and area.startswith("Y") and area.endswith("-Z00"):
            terminals.append({
                "terminal": _terminal_name(desc[area][0]), "eia_area": area,
                "mmcf": mmcf, "bcf_per_day": _bcf_per_day(mmcf, last), "share_pct": round(mmcf / us * 100, 1),
                "yoy_pct": _pct(mmcf, vol.get((area, year_ago))),
                "export_price_usd_per_mcf": price.get((area, last)),
            })
    terminals.sort(key=lambda t: -t["mmcf"])

    destinations = []
    for (area, period), mmcf in vol.items():
        if period == last and area.startswith("NUS-N") and mmcf > 0:
            destinations.append({
                "country": _country_name(desc[area][0]), "iso3": desc[area][1],
                "mmcf": mmcf, "share_pct": round(mmcf / us * 100, 1),
                "yoy_pct": _pct(mmcf, vol.get((area, year_ago))),
            })
    destinations.sort(key=lambda d: -d["mmcf"])

    trailing = [vol.get(("NUS-Z00", _shift(last, -i))) for i in range(12)]
    history = [{"period": p, "mmcf": vol[("NUS-Z00", p)], "bcf_per_day": _bcf_per_day(vol[("NUS-Z00", p)], p)}
               for p in reversed(us_periods[-24:])]

    return {
        "dataset": "AurAgrid U.S. LNG Export Monitor",
        "period": last,
        "frequency": "monthly",
        "notes": "U.S. LNG exports by terminal and destination from EIA monthly data, published with a lag "
                 "of about three months. Volumes in MMcf (million cubic feet); Bcf/d = daily average.",
        "us_total": {
            "mmcf": us, "bcf_per_day": _bcf_per_day(us, last),
            "mom_pct": _pct(us, vol.get(("NUS-Z00", prev))),
            "yoy_pct": _pct(us, vol.get(("NUS-Z00", year_ago))),
            "trailing_12m_mmcf": sum(trailing) if None not in trailing else None,
            "export_price_usd_per_mcf": price.get(("NUS-Z00", last)),
        },
        "terminals": terminals,
        "destinations": destinations,
        "history": history,
        "attribution": {
            "source": "%s (data through %s; accessed %s)." % (SOURCE, last, date.today().isoformat()),
            "terms": "EIA data is in the U.S. public domain. EIA does not endorse AurAgrid.",
            "transformation": "AurAgrid computes Bcf/d, shares and percentage changes; EIA values are unaltered.",
        },
    }


feeds.describe(
    "lng-supply-chain", source="U.S. Energy Information Administration (EIA), natural gas exports by point of exit",
    url="https://api.eia.gov/v2/natural-gas/move/poe2/", terms_url="https://www.eia.gov/about/copyrights_reuse.php",
    attribution="Source: U.S. Energy Information Administration. Public domain; EIA does not endorse AurAgrid.",
    as_of=lambda snap: snap["period"])


def register_from_env() -> bool:
    key = os.environ.get("EIA_API_KEY")
    if not key:
        log.warning("EIA_API_KEY not set: lng-supply-chain stays 'Feed not connected'")
        return False
    session = requests.Session()
    session.headers["User-Agent"] = "AurAgrid-EIA/1.0"

    def fetch():
        today = date.today()
        start = _shift("%04d-%02d" % (today.year, today.month), -(MONTHS + 6))   # covers the publication lag
        return build_snapshot(fetch_rows(session, key, start))

    feeds.provider("lng-supply-chain")(feeds.CachedFeed(
        fetch,
        float(os.environ.get("AURAGRID_EIA_CACHE_SECONDS", str(12 * 3600))),
        float(os.environ.get("AURAGRID_EIA_MAX_STALE_SECONDS", str(45 * 86400))),
        retry_seconds=900))
    log.info("lng-supply-chain provider registered (U.S. EIA)")
    return True
