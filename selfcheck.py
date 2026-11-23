"""Weekly self-check (machine-discovery Phase 7): read-only, never pays, never restarts anything.

    python selfcheck.py [--base https://84.12.79.124] [--out /var/log/auragrid/weekly.txt]

From the PUBLIC URL, the way a directory or an agent sees AurAgrid:
- discovery files parse and agree with each other (/openapi.json, /llms.txt, manifest,
  /.well-known/l402-services, /status.json, /robots.txt);
- every free endpoint and free sample answers 200 with an as_of (stale is a WARN);
- every paid endpoint answers a 402 L402 challenge (the invoice is never paid);
- every advertised x402 endpoint answers 402 with PAYMENT-REQUIRED (never paid);
- the TLS certificate has at least 3 days left;
- every listing in LISTINGS_TO_CHECK still resolves.
Writes one line per check to --out (replaced each run) and exits 1 if anything FAILed.
The dashboard shows the FAIL lines of the latest report.
"""
import argparse
import json
import os
import socket
import ssl
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

DEFAULT_OUT = "/var/log/auragrid/weekly.txt"
EXAMPLE_PARAMS = {"lat": "29.5", "lon": "-90", "radius_km": "500"}
PACE_SECONDS = 2.1        # stay under the free tier's per-IP rate limit
TLS_WARN_DAYS = 3
# Public listing pages: (label, url, text that must appear, pending review?). A pending
# listing is not public yet, so a 404 there is a WARN until it is approved (see LISTINGS.md).
LISTINGS_TO_CHECK = [
    ("satring", "https://satring.com/services/auragrid-bitcoin-fees-lightning-network-data", "AurAgrid", False),
    ("402index btc-fees", "https://402index.io/api/v1/services/22131ab3-de4b-42c2-b58e-f1f5852ca889",
     "84.12.79.124", True),
    ("402index ln-network", "https://402index.io/api/v1/services/ad2d6d6e-ea85-4e8b-8267-b3fc3e107c06",
     "84.12.79.124", True),
]


class Report:
    def __init__(self):
        self.lines = []

    def add(self, level, name, detail=""):
        self.lines.append("%s %-5s %s%s" % (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), level, name,
                                            (": " + detail) if detail else ""))

    def counts(self):
        return {k: sum(1 for line in self.lines if " %s " % k.ljust(5) in line) for k in ("PASS", "WARN", "FAIL")}


def get(session, url, **kw):
    return session.get(url, timeout=20, allow_redirects=False, **kw)


def check_discovery(s, base, rep):
    docs = {}
    for path in ("/agent-manifest.json", "/openapi.json", "/.well-known/l402-services", "/status.json"):
        try:
            r = get(s, base + path)
            docs[path] = r.json() if r.status_code == 200 else None
            rep.add("PASS" if docs[path] is not None else "FAIL", "discovery " + path, "HTTP %d" % r.status_code)
        except Exception as exc:
            docs[path] = None
            rep.add("FAIL", "discovery " + path, str(exc)[:120])
    for path, must in (("/llms.txt", "/agent-manifest.json"), ("/robots.txt", "Allow: /llms.txt")):
        try:
            r = get(s, base + path)
            rep.add("PASS" if r.status_code == 200 and must in r.text else "FAIL", "discovery " + path,
                    "HTTP %d" % r.status_code)
        except Exception as exc:
            rep.add("FAIL", "discovery " + path, str(exc)[:120])

    manifest, spec = docs.get("/agent-manifest.json"), docs.get("/openapi.json")
    if manifest:
        n = len(manifest.get("endpoints", []))
        rep.add("PASS" if manifest.get("endpoints_count") == n else "FAIL", "manifest endpoints_count",
                "%s listed, count says %s" % (n, manifest.get("endpoints_count")))
    if manifest and spec:
        documented = set(spec.get("paths", {}))
        missing = [e["path"] for e in manifest.get("endpoints", []) if e["path"] not in documented]
        rep.add("PASS" if not missing else "FAIL", "openapi covers the manifest",
                "missing: " + ", ".join(missing) if missing else "%d paths" % len(documented))
        ok = str(spec.get("openapi", "")).startswith("3.")
        rep.add("PASS" if ok else "FAIL", "openapi version", str(spec.get("openapi")))
    l402 = docs.get("/.well-known/l402-services")
    if manifest and l402:
        paid = {e["path"] for e in manifest.get("endpoints", []) if e.get("access") == "paid"}
        listed = {urlparse(x["url"]).path for x in l402.get("services", [])}
        rep.add("PASS" if paid <= listed else "FAIL", "l402-services lists every paid endpoint",
                ", ".join(sorted(paid - listed)) or "%d services" % len(listed))
    return manifest or {}


def check_endpoints(s, base, manifest, rep):
    for e in manifest.get("endpoints", []):
        time.sleep(PACE_SECONDS)
        path, label = e["path"], e["path"].rsplit("/", 1)[-1]
        try:
            if e.get("access") == "paid":
                r = get(s, base + path)
                h = r.headers.get("WWW-Authenticate", "")
                if r.status_code == 402 and "invoice=" in h and "macaroon=" in h:
                    rep.add("PASS", "402 challenge " + label)
                elif r.status_code == 503:
                    rep.add("WARN", "402 challenge " + label, "503 stale: no invoice issued (as_of %s)"
                            % (r.json().get("as_of") if "json" in r.headers.get("content-type", "") else "?"))
                else:
                    rep.add("FAIL", "402 challenge " + label, "HTTP %d" % r.status_code)
                if e.get("sample_url"):
                    time.sleep(PACE_SECONDS)
                    check_data(s, base, e["sample_url"], "sample " + label, None, rep)
                continue
            params = {k: EXAMPLE_PARAMS.get(k, "") for k in (e.get("parameters") or {})}
            check_data(s, base, path, "free " + label, params or None, rep)
        except Exception as exc:
            rep.add("FAIL", label, str(exc)[:120])


def check_x402(s, base, manifest, rep):
    """Each advertised x402 endpoint answers 402 with a PAYMENT-REQUIRED header (never paid)."""
    for e in (manifest.get("x402") or {}).get("endpoints", []):
        time.sleep(PACE_SECONDS)
        path = urlparse(e["url"]).path
        label = "x402 challenge " + path.rsplit("/", 1)[-1]
        try:
            r = get(s, base + path)
            if r.status_code == 402 and r.headers.get("payment-required"):
                rep.add("PASS", label)
            elif r.status_code == 503:
                rep.add("WARN", label, "503 stale: no payment requested")
            else:
                rep.add("FAIL", label, "HTTP %d" % r.status_code)
        except Exception as exc:
            rep.add("FAIL", label, str(exc)[:120])


def check_data(s, base, path, label, params, rep):
    r = get(s, base + path, params=params)
    if r.status_code == 429:
        rep.add("WARN", label, "rate limited")
        return
    if r.status_code != 200:
        rep.add("FAIL" if r.status_code != 503 else "WARN", label, "HTTP %d" % r.status_code)
        return
    body = r.json()
    prov = body.get("provenance") or {}
    if not prov.get("as_of"):
        rep.add("FAIL", label, "no provenance.as_of")
    elif body.get("stale"):
        rep.add("WARN", label, "served stale (as_of %s)" % prov["as_of"])
    else:
        rep.add("PASS", label, "as_of %s" % prov["as_of"])


def check_tls(base, rep):
    host = urlparse(base).hostname
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                not_after = ssl.cert_time_to_seconds(tls.getpeercert()["notAfter"])
        days = (not_after - time.time()) / 86400
        rep.add("PASS" if days >= TLS_WARN_DAYS else "WARN", "tls certificate", "%.1f days left" % days)
    except Exception as exc:
        rep.add("FAIL", "tls certificate", str(exc)[:120])


def check_listings(s, rep):
    for label, url, must, pending in LISTINGS_TO_CHECK:
        try:
            r = s.get(url, timeout=30)
            if r.status_code == 404 and pending:
                rep.add("WARN", "listing " + label, "not public yet (pending review)")
                continue
            rep.add("PASS" if r.status_code == 200 and must in r.text else "FAIL", "listing " + label,
                    "HTTP %d" % r.status_code)
        except Exception as exc:
            rep.add("FAIL", "listing " + label, str(exc)[:120])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=os.environ.get("AURAGRID_PUBLIC_URL", "https://84.12.79.124"))
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    s = requests.Session()
    s.headers["User-Agent"] = "AurAgrid-selfcheck/1.0"
    rep = Report()
    manifest = check_discovery(s, base, rep)
    check_endpoints(s, base, manifest, rep)
    check_x402(s, base, manifest, rep)
    check_tls(base, rep)
    check_listings(s, rep)
    c = rep.counts()
    rep.add("INFO", "summary", "%d PASS, %d WARN, %d FAIL" % (c["PASS"], c["WARN"], c["FAIL"]))
    text = "\n".join(rep.lines) + "\n"
    tmp = args.out + ".tmp"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, args.out)
    sys.stdout.write(text)
    return 1 if c["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
