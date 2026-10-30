"""port-congestion-index provider: port activity from IMF PortWatch (satellite AIS).

What it measures (and what it does not): PortWatch publishes daily port calls
and import/export volume estimates per port, derived from AIS vessel signals,
plus daily transits through the main maritime chokepoints. It has no berth
wait times or anchorage queues, so this is a port *activity* index: the last
7 days of calls and tonnage for the 100 busiest covered ports, against each
port's previous 90 days. Ratios well below 1 flag disruption or slowdown,
well above 1 a surge. PortWatch updates weekly; recent days may be revised.

Terms: IMF Data terms (imf.org/en/About/copyright-and-terms) allow
redistribution and derived works with attribution, but ask for permission
before commercial reuse or automated downloading, and require telling buyers
the data is free from the IMF. So the provider is only registered when
AURAGRID_PORTWATCH_ENABLED=1, which you should set once copyright@imf.org has
granted permission. Every snapshot carries the attribution and notices.

Refreshing runs in a background thread (feeds.BackgroundFeed): a rebuild takes
minutes, so requests always get the last finished snapshot, which is also
saved to disk for restarts.

Load on PortWatch: every AURAGRID_PORTWATCH_CACHE_SECONDS (default 6h) one
query checks for new data. Only when a new week is published are ~11 small
aggregate queries run for the 100 ranked ports; choosing those 100 (scanning
the 600 largest ports) runs monthly. Per-port daily rows are never downloaded.
"""
import json
import logging
import os
import time
from datetime import date, timedelta

import requests

import feeds

log = logging.getLogger("auragrid.portwatch")

BASE = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services"
PORTS_DAILY = BASE + "/Daily_Ports_Data/FeatureServer/0/query"
PORTS_DB = BASE + "/PortWatch_ports_database/FeatureServer/0/query"
CHOKEPOINTS_DAILY = BASE + "/Daily_Chokepoints_Data/FeatureServer/0/query"
TOP_N, CANDIDATES = 100, 600
RANK_MAX_AGE_SECONDS = 30 * 86400
RECENT_DAYS, BASELINE_DAYS = 7, 90

ATTRIBUTION = {
    "source": "Source: International Monetary Fund, PortWatch (portwatch.imf.org). Based on Arslanalp, Koepke "
              "and Verschuur, 'Tracking Trade from Space', IMF Working Paper 2021/225.",
    "free_from_source": "The underlying PortWatch data is available free of charge from the IMF at portwatch.imf.org.",
    "transformation": "AurAgrid aggregates PortWatch daily series into 7-day totals and ratios against each "
                      "port's prior 90 days; the daily values themselves are not altered.",
    "terms": "https://www.imf.org/en/About/copyright-and-terms",
}


class ArcGISError(RuntimeError):
    pass


def query(session, url: str, params: dict, attempts: int = 3, sleep=time.sleep) -> list:
    """POST an ArcGIS query, retrying transient failures (the service is occasionally flaky)."""
    data = {"f": "json", **params}
    last = None
    for attempt in range(attempts):
        try:
            # GET unless the query string gets long (big id lists): some layers reject POSTed
            # sort/paging parameters, while very long URLs are rejected the other way.
            long_query = sum(len(str(v)) for v in data.values()) > 1500
            resp = session.post(url, data=data, timeout=60) if long_query else \
                session.get(url, params=data, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            if "error" in body:
                raise ArcGISError("ArcGIS error %s: %s" % (body["error"].get("code"), body["error"].get("message")))
            return [f["attributes"] for f in body.get("features") or []]
        except Exception as exc:
            last = exc
            if attempt + 1 < attempts:
                sleep(2 * (attempt + 1))
    raise ArcGISError("PortWatch query failed after %d attempts: %s" % (attempts, last))


def _stats(*specs):
    return json.dumps([{"statisticType": t, "onStatisticField": f, "outStatisticFieldName": n} for t, f, n in specs])


def _ratio(recent, baseline):
    return round(recent / baseline, 3) if baseline else None


def latest_date(session) -> date:
    rows = query(session, PORTS_DAILY, {"where": "1=1", "outStatistics": _stats(("max", "date", "d"))})
    return date.fromisoformat(str(rows[0]["d"])[:10])


def _window_stats(session, url, id_field, ids, start, end, specs, group_fields):
    where = "date >= DATE '%s' AND date <= DATE '%s'" % (start, end)
    if ids:
        where = "%s IN (%s) AND %s" % (id_field, ",".join("'%s'" % i.replace("'", "") for i in ids), where)
    rows = query(session, url, {"where": where, "groupByFieldsForStatistics": group_fields,
                                "outStatistics": _stats(*specs)})
    return {r[id_field]: r for r in rows}


def _chunks(items, n):
    return [items[i:i + n] for i in range(0, len(items), n)]


SPECS = [("sum", "portcalls", "calls"), ("sum", "import", "imp"), ("sum", "export", "exp"),
         ("sum", "portcalls_tanker", "tanker"), ("sum", "portcalls_container", "container"),
         ("count", "portcalls", "days")]


def _windows(last: date):
    recent_start = last - timedelta(days=RECENT_DAYS - 1)
    base_end = recent_start - timedelta(days=1)
    return recent_start, base_end - timedelta(days=BASELINE_DAYS - 1), base_end


def _batched_stats(session, ids, start, end, batch, tolerate_failures=False):
    """Per-port window stats. Date-only filters make PortWatch scan its whole daily table
    and time out, so every daily query is filtered by a small batch of port ids."""
    out = {}
    for chunk in _chunks(list(ids), batch):
        try:
            out.update(_window_stats(session, PORTS_DAILY, "portid", chunk, start, end, SPECS, "portid"))
        except ArcGISError:
            if not tolerate_failures:
                raise
            log.warning("PortWatch batch of %d ports failed; skipped for this ranking", len(chunk))
    return out


def rank_ports(session, last: date) -> list:
    """Top TOP_N port ids by 90-day traffic among the CANDIDATES largest PortWatch ports.
    Slow (many batches), so it runs about monthly; see IncrementalSnapshot."""
    _, base_start, base_end = _windows(last)
    candidates = query(session, PORTS_DB, {"where": "1=1", "outFields": "portid",
                                           "orderByFields": "vessel_count_total DESC",
                                           "resultRecordCount": CANDIDATES})
    base = _batched_stats(session, [c["portid"] for c in candidates], base_start, base_end, 50,
                          tolerate_failures=True)
    ranked = sorted((pid for pid, b in base.items() if b.get("days")),
                    key=lambda pid: -base[pid]["calls"] / base[pid]["days"])
    if len(ranked) < TOP_N // 2:
        raise RuntimeError("Only %d PortWatch ports with daily data found" % len(ranked))
    return ranked[:TOP_N]


def build_snapshot(session, last: date, ranked: list) -> dict:
    recent_start, base_start, base_end = _windows(last)
    base = _batched_stats(session, ranked, base_start, base_end, 25)
    recent_all = _batched_stats(session, ranked, recent_start, last, 25)
    meta = {}
    for chunk in _chunks(ranked, 50):
        meta.update((m["portid"], m) for m in query(session, PORTS_DB, {
            "where": "portid IN (%s)" % ",".join("'%s'" % i for i in chunk),
            "outFields": "portid,portname,country,ISO3,LOCODE"}))

    ports = []
    for pid in ranked:
        b, r = base.get(pid), recent_all.get(pid)
        if not b or not r or not b.get("days") or not r.get("days"):
            continue
        bd, rd = b["days"], r["days"]
        m = meta.get(pid, {})
        ports.append({
            "port_id": pid, "port": m.get("portname"), "country": m.get("country"), "iso3": m.get("ISO3"),
            "locode": m.get("LOCODE"),
            "port_calls_7d": r["calls"], "port_calls_daily_avg_7d": round(r["calls"] / rd, 2),
            "port_calls_daily_avg_90d": round(b["calls"] / bd, 2),
            "activity_ratio": _ratio(r["calls"] / rd, b["calls"] / bd),
            "import_tonnes_7d": r["imp"], "import_ratio": _ratio(r["imp"] / rd, b["imp"] / bd),
            "export_tonnes_7d": r["exp"], "export_ratio": _ratio(r["exp"] / rd, b["exp"] / bd),
            "tanker_calls_7d": r["tanker"], "container_calls_7d": r["container"],
        })
    if len(ports) < TOP_N // 2:
        raise RuntimeError("PortWatch returned only %d usable ports" % len(ports))
    for rank, p in enumerate(ports, 1):
        p["rank"] = rank

    def pooled(field):
        rec = sum(p[field + "_7d"] for p in ports) / RECENT_DAYS
        bas = sum(base[p["port_id"]][{"port_calls": "calls", "import_tonnes": "imp",
                                      "export_tonnes": "exp"}[field]] for p in ports) / BASELINE_DAYS
        return _ratio(rec, bas)

    cp_specs = [("sum", "n_total", "n"), ("sum", "n_tanker", "tanker"), ("sum", "n_container", "container"),
                ("sum", "capacity", "cap"), ("count", "n_total", "days")]
    cp_base = _window_stats(session, CHOKEPOINTS_DAILY, "portname", None, base_start, base_end, cp_specs, "portname")
    cp_recent = _window_stats(session, CHOKEPOINTS_DAILY, "portname", None, recent_start, last, cp_specs, "portname")
    chokepoints = []
    for name, r in sorted(cp_recent.items()):
        b = cp_base.get(name)
        if not b or not b.get("days") or not r.get("days"):
            continue
        chokepoints.append({
            "chokepoint": name, "transits_7d": r["n"], "tanker_transits_7d": r["tanker"],
            "container_transits_7d": r["container"],
            "transit_ratio": _ratio(r["n"] / r["days"], b["n"] / b["days"]),
            "capacity_ratio": _ratio(r["cap"] / r["days"], b["cap"] / b["days"]),
        })

    return {
        "index": "AurAgrid Top 100 Port Activity Index",
        "data_as_of": last.isoformat(),
        "recent_window": [recent_start.isoformat(), last.isoformat()],
        "baseline_window": [base_start.isoformat(), base_end.isoformat()],
        "notes": "Ratios compare daily averages: last 7 days vs the prior 90 days (1.0 = normal). "
                 "Activity, not queue length: PortWatch has no berth wait-time data. "
                 "PortWatch updates weekly and the most recent days may be revised.",
        "global": {"ports": len(ports), "activity_ratio": pooled("port_calls"),
                   "import_ratio": pooled("import_tonnes"), "export_ratio": pooled("export_tonnes")},
        "chokepoints": chokepoints,
        "ports": ports,
        "attribution": ATTRIBUTION,
    }


class IncrementalSnapshot:
    """Rebuild only when PortWatch publishes a newer day (one cheap query otherwise), and
    re-rank the top ports only monthly. The ranking is saved to `state_path` so a restart
    does not trigger the slow ranking step again."""

    def __init__(self, session, build=build_snapshot, rank=rank_ports, state_path=None, clock=time.time):
        self.session, self.build, self.rank, self.clock = session, build, rank, clock
        self.state_path, self.last = state_path, None
        self.ranking = self._load()

    def _load(self):
        if not self.state_path:
            return None
        try:
            with open(self.state_path) as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    def _save(self):
        if self.state_path:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(self.ranking, fh)
            os.replace(tmp, self.state_path)

    def __call__(self) -> dict:
        newest = latest_date(self.session)
        if self.ranking is None or self.clock() - self.ranking["ranked_at"] > RANK_MAX_AGE_SECONDS:
            self.ranking = {"ranked_at": self.clock(), "ports": self.rank(self.session, newest)}
            self._save()
        if self.last is None or self.last["data_as_of"] != newest.isoformat():
            self.last = self.build(self.session, newest, self.ranking["ports"])
        return self.last


def register_from_env() -> bool:
    if os.environ.get("AURAGRID_PORTWATCH_ENABLED") != "1":
        log.warning("AURAGRID_PORTWATCH_ENABLED is not 1: port-congestion-index stays 'Feed not connected' "
                    "(enable after the IMF grants permission for commercial reuse)")
        return False
    session = requests.Session()
    session.headers["User-Agent"] = "AurAgrid-PortWatch/1.0 (aggregate queries, refreshed only when new data is published)"
    snapshots = IncrementalSnapshot(session, state_path=os.environ.get("AURAGRID_PORTWATCH_STATE",
                                                                    "portwatch_ranking.json"))
    feed = feeds.BackgroundFeed(
        snapshots,
        interval=float(os.environ.get("AURAGRID_PORTWATCH_CACHE_SECONDS", str(6 * 3600))),
        max_stale=float(os.environ.get("AURAGRID_PORTWATCH_MAX_STALE_SECONDS", str(10 * 86400))),
        retry_seconds=600,
        state_path=os.environ.get("AURAGRID_PORTWATCH_SNAPSHOT", "portwatch_snapshot.json"),
        name="portwatch")
    snapshots.last = feed.current()   # saved snapshot: skip the rebuild if PortWatch has nothing newer
    feeds.provider("port-congestion-index")(feed.start())
    log.info("port-congestion-index provider registered (IMF PortWatch)")
    return True
