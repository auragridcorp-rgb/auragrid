"""Operator monitoring for the dashboard: service health and public traffic.

Everything here is read-only and local: systemd state, running processes, the
Aperture TLS certificate, and Aperture's access log. Nothing is exposed
publicly; app/main.py serves it under the admin-only /api/proxy/* routes.

Traffic
-------
Aperture logs one line per proxied request:

    2026-11-11 08:48:05.137 [INF] PRXY: 1.2.3.4 - - "GET /path HTTP/1.1" "" "UA"

TrafficLog tails that file incrementally (it re-reads from the start, plus the
rotated .1.gz, when the file is rotated or truncated) and keeps per-day
aggregates for a rolling window. Status codes are not in the log, so the
paywall funnel joins paid-route requests with LND invoices in app/main.py.
"""
import gzip
import os
import re
import shutil
import subprocess
import threading
import time
from collections import Counter, deque
from datetime import datetime, timedelta, timezone

APERTURE_DIR = os.path.expanduser(os.environ.get("AURAGRID_APERTURE_DIR", "~/.aperture"))
APERTURE_LOG = os.environ.get("AURAGRID_APERTURE_LOG", os.path.join(APERTURE_DIR, "aperture.log"))
TLS_CERT = os.environ.get("AURAGRID_TLS_CERT", os.path.join(APERTURE_DIR, "tls.cert"))

# systemd units and what "healthy" means for each.
UNITS = [
    ("aperture", "L402 gateway (:443)", "running"),
    ("auragrid-api", "FastAPI backend", "running"),
    ("auragrid-fetcher", "Feed fetcher", "running"),
    ("auragrid-pricer", "USD pricer", "running"),
    ("lnd", "Lightning node", "running"),
    ("auragrid-dvm", "NIP-90 DVM worker", "running"),
    ("auragrid-cert-renew.timer", "TLS renewal timer", "waiting"),
]
# Long-running processes that are not systemd units.
PROCESSES = [
    ("bitcoind", "Bitcoin Core", r"(^|/)bitcoind( |$)"),
]

CATEGORIES = ("paid", "free", "quote", "discovery", "noise")
DISCOVERY = {"/agent-manifest.json", "/.well-known/ai-plugin.json", "/llms.txt",
             "/openapi.json", "/buy", "/terms"}

LINE_RE = re.compile(
    r'^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)\.\d+ \[\w+\] PRXY: (\S+) - - '
    r'"(\S+) (\S+)[^"]*" "[^"]*" "([^"]*)"')
START_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\.\d+ \[INF\] APER: Starting the server")


def _run(cmd, timeout=5):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _parse_systemd_time(value):
    """'Wed 2026-11-11 00:24:16 UTC' -> epoch seconds, or None."""
    m = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", value or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()


def service_status():
    names = [u[0] for u in UNITS] + ["auragrid-cert-renew.service"]
    out = _run(["systemctl", "show", *names, "-p",
                "Id,ActiveState,SubState,Result,ActiveEnterTimestamp,NRestarts,"
                "ExecMainExitTimestamp,NextElapseUSecRealtime"])
    props = {}
    for block in out.strip().split("\n\n"):
        kv = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if kv.get("Id"):
            props[kv["Id"]] = kv

    rows = []
    for unit, label, want in UNITS:
        p = props.get(unit if "." in unit else unit + ".service", {})
        row = {"name": unit, "label": label, "kind": "systemd",
               "state": p.get("SubState") or "unknown",
               "ok": p.get("SubState") == want,
               "since": _parse_systemd_time(p.get("ActiveEnterTimestamp")),
               "restarts": int(p["NRestarts"]) if p.get("NRestarts", "").isdigit() else None}
        if unit == "auragrid-cert-renew.timer":
            renew = props.get("auragrid-cert-renew.service", {})
            row["last_run"] = _parse_systemd_time(renew.get("ExecMainExitTimestamp"))
            row["last_result"] = renew.get("Result")
            row["next_run"] = _parse_systemd_time(p.get("NextElapseUSecRealtime"))
            row["ok"] = row["ok"] and renew.get("Result", "success") == "success"
        rows.append(row)

    ps = _run(["ps", "-eo", "pid=,etimes=,args="])
    for name, label, pattern in PROCESSES:
        rx = re.compile(pattern)
        match = None
        for line in ps.splitlines():
            parts = line.strip().split(None, 2)
            if len(parts) == 3 and rx.search(parts[2]) and "tee " not in parts[2] and "bash -c" not in parts[2]:
                match = parts
                break
        rows.append({"name": name, "label": label, "kind": "process",
                     "state": "running" if match else "not running", "ok": bool(match),
                     "since": time.time() - int(match[1]) if match else None, "restarts": None})
    return rows


def cert_status(path=TLS_CERT):
    try:
        from cryptography import x509
        with open(path, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())
    except Exception as exc:  # missing file, bad PEM, no cryptography
        return {"ok": False, "error": str(exc)}
    not_after = cert.not_valid_after_utc.timestamp()
    days = (not_after - time.time()) / 86400
    # Short-lived (~6 day) certs renew at about half their lifetime, so under
    # 2 days left means renewal is failing.
    return {"ok": days > 2, "not_after": not_after, "days_left": round(days, 1),
            "not_before": cert.not_valid_before_utc.timestamp()}


def disk_status(path="/"):
    u = shutil.disk_usage(path)
    pct = round(u.used / u.total * 100, 1)
    return {"ok": pct < 90, "used_pct": pct, "free_gb": round(u.free / 2**30, 1)}


class TrafficLog:
    """Rolling per-day aggregates of Aperture's access log."""

    def __init__(self, path, classify, self_ips=(), days=14, min_interval=15.0, clock=time.time,
                 sample_paths=()):
        self.path, self.classify, self.days = path, classify, days
        self.sample_paths = set(sample_paths)   # free samples of paid feeds (counted as "sample_hits")
        self.self_ips = set(self_ips) | {"127.0.0.1", "::1"}
        self.min_interval, self.clock = min_interval, clock
        self._lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.offset, self.inode, self.head, self.last_refresh = 0, None, b"", 0.0
        self.by_day = {}          # day -> {"counts": Counter, "ips": {cat: set}, "paths": {cat: Counter}}
        self.recent = deque(maxlen=60)
        self.starts = deque(maxlen=20)
        self.self_requests = 0

    def _day(self, day):
        d = self.by_day.get(day)
        if d is None:
            d = self.by_day[day] = {"counts": Counter(), "ips": {c: set() for c in CATEGORIES},
                                    "paths": {c: Counter() for c in CATEGORIES}}
        return d

    def _cutoff_day(self):
        now = datetime.fromtimestamp(self.clock(), timezone.utc)
        return (now - timedelta(days=self.days - 1)).strftime("%Y-%m-%d")

    def feed_line(self, line, cutoff):
        if "PRXY:" not in line:
            m = START_RE.match(line)
            if m:
                self.starts.append(m.group(1))
            return
        m = LINE_RE.match(line)
        if not m:
            return
        day, hms, ip, method, target, ua = m.groups()
        if day < cutoff:
            return
        if ip in self.self_ips:
            self.self_requests += 1
            return
        path = target.split("?", 1)[0]
        cat = self.classify(path) if method in ("GET", "HEAD", "POST") else "noise"
        d = self._day(day)
        d["counts"][cat] += 1
        if path in self.sample_paths:
            d["counts"]["sample"] += 1
        d["ips"][cat].add(ip)
        d["paths"][cat][path[:120]] += 1
        if cat != "noise":
            self.recent.append({"time": day + " " + hms, "ip": ip, "method": method,
                                "path": path[:200], "category": cat, "user_agent": ua[:160]})

    def refresh(self, force=False):
        with self._lock:
            if not force and self.clock() - self.last_refresh < self.min_interval:
                return
            self.last_refresh = self.clock()
            try:
                st = os.stat(self.path)
            except OSError:
                return
            cutoff = self._cutoff_day()
            with open(self.path, "rb") as f:
                head = f.read(256)
            # Rotated or truncated: new inode, shorter file, or different first line
            # (an inode number can be reused by the new file).
            if st.st_ino != self.inode or st.st_size < self.offset or head[:len(self.head)] != self.head:
                self._reset()
                self.last_refresh = self.clock()
                self.inode = st.st_ino
                rotated = self.path + ".1.gz"
                if os.path.exists(rotated):
                    try:
                        with gzip.open(rotated, "rt", errors="replace") as f:
                            for line in f:
                                self.feed_line(line, cutoff)
                    except (OSError, EOFError):
                        pass
            with open(self.path, "rb") as f:
                f.seek(self.offset)
                chunk = f.read()
            end = chunk.rfind(b"\n")
            if end < 0:
                return
            self.offset += end + 1   # an unfinished last line is read next time
            if len(self.head) < 256:
                self.head = head
            for line in chunk[:end].decode("utf-8", "replace").splitlines():
                self.feed_line(line, cutoff)
            for day in [d for d in self.by_day if d < cutoff]:
                del self.by_day[day]

    def summary(self, days=7):
        self.refresh()
        with self._lock:
            now = datetime.fromtimestamp(self.clock(), timezone.utc)
            span = [(now - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days - 1, -1, -1)]
            daily, totals, ips, paths = [], Counter(), {c: set() for c in CATEGORIES}, \
                {c: Counter() for c in CATEGORIES}
            for day in span:
                d = self.by_day.get(day)
                counts = d["counts"] if d else Counter()
                daily.append({"day": day, **{c: counts.get(c, 0) for c in CATEGORIES},
                              "clients": len(set().union(*(d["ips"][c] for c in CATEGORIES if c != "noise")))
                              if d else 0})
                if d:
                    totals.update(counts)
                    for c in CATEGORIES:
                        ips[c] |= d["ips"][c]
                        paths[c].update(d["paths"][c])
            real_ips = set().union(*(ips[c] for c in CATEGORIES if c != "noise"))
            return {
                "days": days,
                "daily": daily,
                "totals": {**{c: totals.get(c, 0) for c in CATEGORIES}, "sample_hits": totals.get("sample", 0)},
                "clients": {c: len(ips[c]) for c in CATEGORIES} | {"real": len(real_ips)},
                "top_paths": {c: [{"path": p, "requests": n} for p, n in paths[c].most_common(8)]
                              for c in CATEGORIES},
                "recent": [r for r in reversed(self.recent) if r["time"] >= span[0]][:30],
                "aperture_starts": list(self.starts)[-5:],
                "self_requests": self.self_requests,
                "log_path": self.path,
            }
