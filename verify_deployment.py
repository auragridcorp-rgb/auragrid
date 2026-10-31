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

RETIRED = ["/api/v1/batch/ercot-lmp", "/api/v1/batch/port-congestion-index", "/api/v1/sample/btc-fees"]
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


def check_retired(base):
    """The old catalog and the old sample/pro routes must answer 410, never data."""
    for path in RETIRED:
        r = get(base, path)
        record("PASS" if r.status_code == 410 else "FAIL", "retired " + path, "HTTP %d" % r.status_code)


def check_signing(base, manifest):
    """When the manifest says responses are signed, a live free endpoint must verify."""
    sig = manifest.get("signature") or {}
    if not sig.get("enabled"):
        record("WARN", "signing", "manifest says response signing is off")
        return
    live = [e["path"] for e in manifest.get("endpoints", []) if e.get("access") == "free" and e.get("status") == "live"]
    if not live:
        record("WARN", "signing", "no live free endpoint to check")
        return
    try:
        import attestation
        nonce = "verify" + datetime.now(timezone.utc).strftime("%H%M%S%f")
        r = get(base, live[0], headers={attestation.NONCE_HEADER: nonce})
        ok = attestation.make_verifier(sig["pubkey"], "v2")(
            r.content, r.headers, {"method": "GET", "target": r.request.path_url, "path": live[0], "nonce": nonce})
        record("PASS" if ok else "FAIL", "signing " + live[0],
               "X-AurAgrid-Signature-V2 verifies against the manifest pubkey" if ok else "signature missing or invalid")
    except Exception as exc:
        record("FAIL", "signing", str(exc))


def check_offer(base, manifest):
    """Every advertised endpoint behaves as its manifest entry says."""
    for e in manifest.get("endpoints", []):
        path, label = e["path"], e["path"].rsplit("/", 1)[-1]
        if e.get("access") == "paid":
            check_paid(base, path, "paywall " + label)
            continue
        r = get(base, path)
        if r.status_code == 402:
            record("FAIL", "free " + label, "free endpoint asks for payment")
        elif e.get("status") == "live":
            record("PASS" if r.status_code == 200 else "FAIL", "free " + label, "HTTP %d" % r.status_code)
        else:
            record("PASS" if r.status_code == 503 else "WARN", "free " + label,
                   "coming soon, HTTP %d" % r.status_code)
    for plan in manifest.get("plans", []):
        check_paid(base, plan["paths"][0], "paywall plan " + plan["name"])
    if not any(e.get("access") == "paid" for e in manifest.get("endpoints", [])) and not manifest.get("plans"):
        print("INFO  nothing is for sale: no paid endpoints or plans in the manifest")


def check_quotes(base, manifest):
    paths = ["/api/v1/price/" + e["path"][len("/api/v1/"):] for e in manifest.get("endpoints", [])
             if e.get("access") == "paid"]
    paths += ["/api/v1/price/" + p["paths"][0][len("/api/v1/"):] for p in manifest.get("plans", [])]
    for path in paths:
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
    for path in ["/agent-manifest.json", "/.well-known/ai-plugin.json", "/openapi.json", "/llms.txt", "/buy", "/terms"]:
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
    ap.add_argument("--paid-path", help="extra path that must answer 402 (paid endpoints in the manifest are checked anyway)")
    ap.add_argument("--aperture-port", type=int, default=8080, help="port Aperture listens on")
    ap.add_argument("--backend-port", type=int, default=8000, help="port the FastAPI backend listens on")
    args = ap.parse_args(argv)
    base = args.base.rstrip("/")
    if base.startswith("http://") and not any(h in base for h in ("127.0.0.1", "localhost")):
        record("WARN", "transport", "base URL is plain http: macaroons/preimages travel unencrypted")
    try:
        manifest = get(base, "/agent-manifest.json").json()
    except Exception as exc:
        record("FAIL", "manifest", str(exc))
        manifest = {}
    if args.paid_path:
        check_paid(base, args.paid_path, "paywall " + args.paid_path)
    check_offer(base, manifest)
    check_retired(base)
    check_signing(base, manifest)
    check_quotes(base, manifest)
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
