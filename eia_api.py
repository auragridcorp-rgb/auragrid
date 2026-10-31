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
             max_rows: int = 50000, page_size: int = PAGE) -> list:
    """All rows of `route`/data for `params`, following EIA's offset paging."""
    rows, offset = [], 0
    while True:
        query = {"api_key": api_key, "data[0]": "value", **params, "offset": offset, "length": page_size}
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
        if len(page) < page_size or len(rows) >= total or len(rows) >= max_rows:
            return rows
        offset += page_size


def latest_period(session, route: str, api_key: str, params: dict) -> str:
    """The newest `period` EIA has published for `route` and `params`."""
    rows = get_rows(session, route, api_key, {**params, "sort[0][column]": "period", "sort[0][direction]": "desc"},
                    max_rows=1, page_size=1)
    if not rows:
        raise EIAError("EIA returned no rows for %s" % route)
    return rows[0]["period"]
