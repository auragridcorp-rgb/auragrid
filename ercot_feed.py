"""ercot-lmp provider: real-time SCED LMPs from ERCOT's official Public API.

Report NP6-788-CD "LMPs by Resource Nodes, Load Zones and Trading Hubs",
GET https://api.ercot.com/api/public-reports/np6-788-cd/lmp_node_zone_hub
(endpoint and parameters from ERCOT's OpenAPI spec, github.com/ercot/api-specs).

Credentials (free registration at ERCOT's API Explorer, apiexplorer.ercot.com):
every call sends the subscription key; if a username/password is configured,
an ID token from ERCOT's Azure B2C login is sent as a Bearer token too and
renewed before its one-hour lifetime runs out. The B2C values below follow
ERCOT's published examples; override them by env if ERCOT changes them.

Environment
-----------
ERCOT_SUBSCRIPTION_KEY   required; the provider is only registered when set
ERCOT_USERNAME           ERCOT API Explorer login (email)
ERCOT_PASSWORD
ERCOT_TOKEN_URL, ERCOT_CLIENT_ID, ERCOT_SCOPE   B2C overrides (rarely needed)
ERCOT_CACHE_SECONDS      how long one snapshot is reused (default 60; SCED runs every ~5 min)
ERCOT_MAX_STALE_SECONDS  serve the last good snapshot (flagged stale) up to this age (default 900)

Each paid request reuses the cached snapshot, so ERCOT is called at most once
per ERCOT_CACHE_SECONDS no matter how many customers there are.
"""
import logging
import os
import time
from statistics import mean
from typing import Optional

import requests

import feeds

log = logging.getLogger("auragrid.ercot")

BASE_URL = "https://api.ercot.com/api/public-reports"
REPORT_PATH = "/np6-788-cd/lmp_node_zone_hub"
SOURCE = "ERCOT NP6-788-CD: LMPs by Resource Nodes, Load Zones and Trading Hubs (real-time SCED)"
TOKEN_URL = os.environ.get(
    "ERCOT_TOKEN_URL",
    "https://ercotb2c.b2clogin.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token")
CLIENT_ID = os.environ.get("ERCOT_CLIENT_ID", "fec253ea-0d06-4272-a5e6-b478baeecd70")
SCOPE = os.environ.get("ERCOT_SCOPE", "openid %s offline_access" % CLIENT_ID)
PAGE_SIZE = 5000
MAX_PAGES = 10


class ERCOTClient:
    def __init__(self, subscription_key: str, username: Optional[str] = None,
                 password: Optional[str] = None, session=None, clock=time.time):
        self.subscription_key, self.username, self.password = subscription_key, username, password
        self.session = session or requests.Session()
        self.clock = clock
        self._token, self._token_expires = None, 0.0

    def _id_token(self) -> Optional[str]:
        if not (self.username and self.password):
            return None
        if self._token and self.clock() < self._token_expires - 300:
            return self._token
        resp = self.session.post(TOKEN_URL, timeout=15, data={
            "username": self.username, "password": self.password, "grant_type": "password",
            "scope": SCOPE, "client_id": CLIENT_ID, "response_type": "id_token"})
        resp.raise_for_status()
        body = resp.json()
        self._token = body.get("id_token") or body.get("access_token")
        if not self._token:
            raise RuntimeError("ERCOT login returned no id_token")
        self._token_expires = self.clock() + int(body.get("expires_in") or 3600)
        return self._token

    def get(self, params: dict) -> dict:
        headers = {"Ocp-Apim-Subscription-Key": self.subscription_key, "Accept": "application/json"}
        token = self._id_token()
        if token:
            headers["Authorization"] = "Bearer " + token
        resp = self.session.get(BASE_URL + REPORT_PATH, params=params, headers=headers, timeout=20)
        if resp.status_code in (401, 403):
            self._token = None   # force a fresh login next time
        resp.raise_for_status()
        return resp.json()


def rows(report: dict) -> list:
    """Report 'data' as a list of dicts, whether ERCOT sends row arrays or objects."""
    data = report.get("data") or []
    if isinstance(data, dict):
        data = data.get("rows") or data.get("data") or []
    names = [f.get("name") for f in report.get("fields") or []]
    return [dict(zip(names, r)) if isinstance(r, (list, tuple)) else dict(r) for r in data]


def _col(row: dict, *wanted):
    lower = {k.lower(): v for k, v in row.items()}
    for name in wanted:
        if name.lower() in lower:
            return lower[name.lower()]
    raise KeyError("none of %s in ERCOT row (columns: %s)" % (wanted, sorted(row)))


def build_snapshot(client: ERCOTClient) -> dict:
    """All settlement point LMPs for the most recent SCED interval."""
    latest_rows = rows(client.get({"sort": "SCEDTimestamp", "dir": "desc", "size": 1}))
    if not latest_rows:
        raise RuntimeError("ERCOT returned no LMP rows")
    stamp = _col(latest_rows[0], "SCEDTimestamp")

    collected = []
    for page in range(1, MAX_PAGES + 1):
        report = client.get({"SCEDTimestampFrom": stamp, "SCEDTimestampTo": stamp,
                             "size": PAGE_SIZE, "page": page})
        collected += rows(report)
        total_pages = int((report.get("_meta") or {}).get("totalPages") or 1)
        if page >= total_pages:
            break

    prices, repeated = {}, False
    for row in collected:
        point = str(_col(row, "settlementPoint", "SettlementPoint"))
        prices[point] = float(_col(row, "LMP"))
        repeated = repeated or bool(_col(row, "repeatHourFlag", "RepeatedHourFlag"))
    if not prices:
        raise RuntimeError("ERCOT returned no rows for SCED interval %s" % stamp)

    def group(prefix):
        return {p: v for p, v in sorted(prices.items()) if p.startswith(prefix)}

    values = list(prices.values())
    return {
        "source": SOURCE,
        "sced_timestamp": stamp,
        "timezone": "America/Chicago",
        "repeated_hour": repeated,
        "settlement_points": len(prices),
        "summary": {"min_lmp": min(values), "max_lmp": max(values), "mean_lmp": round(mean(values), 2),
                    "unit": "USD/MWh"},
        "trading_hubs": group("HB_"),
        "load_zones": group("LZ_"),
        "prices": [{"settlement_point": p, "lmp": v} for p, v in sorted(prices.items())],
    }


def register_from_env() -> bool:
    key = os.environ.get("ERCOT_SUBSCRIPTION_KEY")
    if not key:
        log.warning("ERCOT_SUBSCRIPTION_KEY not set: ercot-lmp stays 'Feed not connected'")
        return False
    client = ERCOTClient(key, os.environ.get("ERCOT_USERNAME"), os.environ.get("ERCOT_PASSWORD"))
    feeds.provider("ercot-lmp")(feeds.CachedFeed(
        lambda: build_snapshot(client),
        float(os.environ.get("ERCOT_CACHE_SECONDS", "60")),
        float(os.environ.get("ERCOT_MAX_STALE_SECONDS", "900"))))
    log.info("ercot-lmp provider registered (ERCOT Public API, NP6-788-CD)")
    return True
