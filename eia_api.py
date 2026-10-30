"""Small shared client for the U.S. EIA Open Data API v2 (paging and retries).

EIA data is U.S. public domain. One free key (EIA_API_KEY) serves every EIA
feed: register at https://www.eia.gov/opendata/register.php.
"""
import time

BASE = "https://api.eia.gov/v2/"
PAGE = 5000   # EIA's maximum rows per request


class EIAError(RuntimeError):
    pass


def get_rows(session, route: str, api_key: str, params: dict, attempts: int = 3, sleep=time.sleep,
             max_rows: int = 50000) -> list:
    """All rows of `route`/data for `params`, following EIA's offset paging."""
    rows, offset = [], 0
    while True:
        query = {"api_key": api_key, "data[0]": "value", **params, "offset": offset, "length": PAGE}
        for attempt in range(attempts):
            try:
                resp = session.get(BASE + route.strip("/") + "/data/", params=query, timeout=30)
                body = resp.json()
                if "error" in body:
                    err = body["error"]
                    raise EIAError("EIA error %s" % (err.get("code", err) if isinstance(err, dict) else err))
                resp.raise_for_status()
                break
            except Exception as exc:
                if attempt + 1 == attempts:
                    raise EIAError("EIA request to %s failed: %s" % (route, exc))
                sleep(2 * (attempt + 1))
        page = body["response"]["data"]
        rows += page
        total = int(body["response"].get("total") or 0)
        if len(page) < PAGE or len(rows) >= total or len(rows) >= max_rows:
            return rows
        offset += PAGE
