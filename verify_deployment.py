"""Black-box preflight for a live AurAgrid deployment. Read-only: it never pays.

Run it from a machine OUTSIDE your server (a laptop, a phone hotspot), because
what matters is what the internet can reach:

    python verify_deployment.py --base https://oracle.example.com --ip 84.12.79.124

Each check prints PASS, WARN or FAIL. Exit code is 1 if anything FAILed.
"""
import argparse
import json
import socket
import sys
from datetime import datetime, timezone

import requests

HERO = ["ercot-lmp", "lng-supply-chain", "port-congestion-index"]
results = []


def record(level, name, detail=""):
    results.append(level)
    print("%-5s %s%s" % (level, name, (": " + detail) if detail else ""))


def get(base, path, **kw):
    return requests.get(base + path, timeout=10, allow_redirects=False, **kw)


def check_paid(base, path, label):
    r = get(base, path)
    header = r.headers.get("WWW-Authenticate", "")
    if r.status_code == 200:
        record("FAIL", label, "returned 200 WITHOUT payment: the paywall is bypassed")
    elif r.status_code == 402 and "invoice=" in header and "macaroon=" in header:
        record("PASS", label, "402 with invoice + macaroon")
    else:
        record("FAIL", label, "expected 402 with an L402 challenge, got HTTP %d" % r.status_code)


def check_samples(base):
    for slug in HERO:
        label = "sample " + slug
        r = get(base, "/api/v1/sample/" + slug)
        if r.status_code == 503:
            record("WARN", label, "warming up (first sample appears after the delay); retry later")
            continue
        if r.status_code != 200:
            record("FAIL", label, "HTTP %d (is /api/v1/sample reachable through Aperture's public-free rule?)" % r.status_code)
            continue
        try:
            body = r.json()
            if body.get("mode") == "schema_example":
                ok = body.get("example") is True and body.get("synthetic") is True and body.get("notice")
                record("WARN" if ok else "FAIL", label,
                       "labeled schema example only: feed not connected" if ok else "example not labeled as synthetic")
                continue
            as_of = datetime.fromisoformat(body["as_of"])
            age = (datetime.now(timezone.utc) - as_of).total_seconds()
            if body.get("mode") != "sample_delayed" or not body.get("data"):
                record("FAIL", label, "unexpected shape")
            elif age < 0 or age > 6 * 3600:
                record("WARN", label, "as_of is %.0f minutes old: the snapshotter may be stuck" % (age / 60))
            else:
                record("PASS", label, "real snapshot, as_of %.0f min ago, %d list(s) truncated" % (age / 60, len(body.get("truncated", {}))))
        except Exception as exc:
            record("FAIL", label, "bad body: %s" % exc)


def check_quotes(base):
    for path in ["/api/v1/price/pro/ercot-lmp", "/api/v1/price/bundle/ercot-lmp"]:
        r = get(base, path)
        try:
            q = r.json()
            ok = r.status_code == 200 and int(q["price_sats"]) > 0
            record("PASS" if ok else "FAIL", "quote " + path.split("/price/")[1],
                   "%s sats (%s%s)" % (q.get("price_sats"), q.get("pricing"), ", STALE" if q.get("stale") else "") if ok
                   else "HTTP %d" % r.status_code)
            if ok and q.get("stale"):
                record("WARN", "quote freshness", "BTC price is stale: check pricer.py and its feeds")
        except Exception as exc:
            record("FAIL", "quote " + path, str(exc))


def check_discovery(base):
    for path in ["/agent-manifest.json", "/.well-known/ai-plugin.json", "/openapi.json", "/llms.txt", "/buy"]:
        r = get(base, path)
        record("PASS" if r.status_code == 200 else ("WARN" if path == "/llms.txt" else "FAIL"), "public " + path,
               "HTTP %d" % r.status_code)
    try:
        m = get(base, "/agent-manifest.json").json()
        if m.get("api", {}).get("url", "").startswith("http://"):
            record("WARN", "manifest api url", "points at plain http: set AURAGRID_PUBLIC_URL to your https domain")
    except Exception:
        pass


def check_operator_locked(base):
    for path in ["/", "/api/proxy/stats", "/api/proxy/transactions", "/api/proxy/info"]:
        r = get(base, path)
        if r.status_code == 200:
            record("FAIL", "operator " + path, "reachable WITHOUT credentials from the internet")
        else:
            record("PASS", "operator " + path, "HTTP %d (not public)" % r.status_code)


def check_ports(ip, aperture_port=8080, backend_port=8000):
    expect_closed = {backend_port: "FastAPI backend (would bypass the paywall)", 10010: "pricer gRPC",
                     10009: "LND gRPC", 8084: "LND REST", 8081: "LND REST / other backends",
                     8082: "other backends", 8083: "other backends", 8000: "old backend port"}
    for port, why in expect_closed.items():
        if port == aperture_port:
            continue
        s = socket.socket(); s.settimeout(3)
        try:
            s.connect((ip, port)); record("FAIL", "port %d open to the internet" % port, why)
        except OSError:
            record("PASS", "port %d closed" % port, why)
        finally:
            s.close()
    s = socket.socket(); s.settimeout(3)
    try:
        s.connect((ip, aperture_port)); record("WARN", "port %d open" % aperture_port, "Aperture is public on plain HTTP: put TLS (Caddy/nginx) in front and close it")
    except OSError:
        record("PASS", "port %d closed" % aperture_port, "Aperture only reachable through your TLS front end")
    finally:
        s.close()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", required=True, help="public base URL, e.g. https://oracle.example.com")
    ap.add_argument("--ip", help="server public IP for the port exposure checks")
    ap.add_argument("--paid-path", default="/api/v1/wholesale/ercot-lmp")
    ap.add_argument("--aperture-port", type=int, default=8080, help="port Aperture listens on")
    ap.add_argument("--backend-port", type=int, default=8000, help="port the FastAPI backend listens on")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    if base.startswith("http://") and not any(h in base for h in ("127.0.0.1", "localhost")):
        record("WARN", "transport", "base URL is plain http: macaroons/preimages travel unencrypted")
    check_paid(base, args.paid_path, "paywall hourly")
    check_paid(base, "/api/v1/pro/ercot-lmp", "paywall 30-day pass")
    check_paid(base, "/api/v1/bundle/ercot-lmp", "paywall bundle")
    check_samples(base)
    check_quotes(base)
    check_discovery(base)
    check_operator_locked(base)
    if args.ip:
        check_ports(args.ip, args.aperture_port, args.backend_port)
    else:
        print("INFO  skipped port checks (pass --ip)")
    print("\n%d PASS, %d WARN, %d FAIL" % tuple(results.count(k) for k in ("PASS", "WARN", "FAIL")))
    return 1 if "FAIL" in results else 0


if __name__ == "__main__":
    sys.exit(main())
