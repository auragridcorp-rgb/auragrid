"""AurAgrid backend: free and paid data feeds, passes/bundles, operator dashboard.

Run behind Aperture only, bound to loopback:

    uvicorn app.main:app --host 127.0.0.1 --port 8000

Routes
------
/api/v1/<feed>                 paid hourly feeds (Aperture sells the token)
/api/v1/<plan prefix>/<feed>   30-day passes and bundles, rewritten onto the
                               feed routes by pass_support.PlanRewriteMiddleware
/api/v1/sample/*, /api/v1/pro/*, retired feeds   410 Gone
Feed data comes from the snapshot store (store.py) written by fetcher.py; the
API never calls an upstream source during a request.
/api/v1/price/<path>           free price quotes
/buy                           browser checkout (WebLN, or paste the preimage)
/agent-manifest.json, /.well-known/ai-plugin.json, /llms.txt, /openapi.json
/, /api/proxy/*                operator only (HTTP Basic, never routed by Aperture)

Environment variables
---------------------
AURAGRID_ADMIN_USER / AURAGRID_ADMIN_PASSWORD
    Required. Protects the dashboard and /api/proxy/* (HTTP Basic). If unset,
    those routes return 503 instead of being open.
AURAGRID_PUBLIC_URL
    Public https base URL used in the agent manifest (e.g. https://oracle.example.com).
    Falls back to the request's base URL if unset.
AURAGRID_SIGNING_PUBKEY
    Oracle node pubkey agents use to verify X-AurAgrid-Signature. Omitted from
    the manifest if unset.

AURAGRID_APERTURE_CONFIG
    Path to aperture.yaml (default: aperture.yaml). Prices and token lifetimes
    in the manifest are read from it, and startup warns about catalog paths no
    Aperture rule covers, and Aperture rules that cover no catalog path.
AURAGRID_BATCH_PREFIX
    URL prefix of the 16 batch feeds under /api/v1/ ("batch" or "wholesale").
AURAGRID_PRICING_CONFIG
    pricing.yaml: USD targets and the `plans:` (passes, bundles).
AURAGRID_GATEWAY_SECRET
    Shared secret Aperture injects as X-AurAgrid-Gateway on paid services
    (gen_aperture.py --gateway-secret-file). Paid routes refuse requests
    without it. If unset, they only require an L402 header, which a client
    that reaches this port directly can forge.

AURAGRID_LEDGER_DB
    SQLite file for the request-time usage ledger (default: ledger.db).
AURAGRID_ATTEST_MACAROON, LND_REST_URL, LND_TLS_CERT
    Enable v2 response attestation on /api/v1/*: path to a macaroon baked with
    only `message:write` (lncli bakemacaroon message:write --save_to=attest.macaroon),
    LND's REST URL, and its TLS certificate.

Revenue attribution
-------------------
1. Exact: l402_ledger records sha256(preimage) -> endpoint, plan (hourly,
   pass_30d, bundle) and call count as paid requests are served; settled
   invoices are joined on payment hash.
2. Fallback for invoices with no ledger row (older, or the header wasn't
   forwarded): service name in the memo, then invoice price.
Check /api/proxy/stats -> "ledger" to confirm the ledger is capturing calls.
Requires PyYAML for reading aperture.yaml.
"""
import json
import logging
import os
import re
import secrets
import threading
import time
from contextlib import asynccontextmanager

from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.openapi.utils import get_openapi
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from typing import Optional

import feeds
from app.buy_page import BUY_HTML
from app.lnd_client import lnd
import attestation
from attestation import AttestationMiddleware, lnd_rest_signer
from l402_ledger import L402LedgerMiddleware, Ledger, hash_hex, token_payment_hash
from pass_support import HOURLY, PlanRewriteMiddleware, PlanRouter, feed_path, load_access, load_plans
from pricer import PriceEngine, normalize_path
import derived
import jobs
import ops_monitor
from store import Store
# Provider modules are imported for their provenance (feeds.describe); the API never
# calls them: fetcher.py fetches, the API reads stored snapshots (jobs.StoreFeed).
import btc_feed  # noqa: F401
import eia_grid_feed  # noqa: F401
import eia_lng_feed  # noqa: F401
import eia_spr_feed  # noqa: F401
import ln_feed  # noqa: F401
import ndbc_feed  # noqa: F401
import usgs_feed  # noqa: F401


log = logging.getLogger("auragrid")

ADMIN_USER = os.environ.get("AURAGRID_ADMIN_USER")
ADMIN_PASSWORD = os.environ.get("AURAGRID_ADMIN_PASSWORD")
PUBLIC_URL = os.environ.get("AURAGRID_PUBLIC_URL", "").rstrip("/")
SIGNING_PUBKEY = os.environ.get("AURAGRID_SIGNING_PUBKEY")
GATEWAY_SECRET = os.environ.get("AURAGRID_GATEWAY_SECRET", "")
GATEWAY_HEADER = "x-auragrid-gateway"

if PUBLIC_URL and not PUBLIC_URL.startswith("https://"):
    log.warning("AURAGRID_PUBLIC_URL is not https: L402 macaroons/preimages "
                "are bearer credentials and must not travel over plain HTTP.")

@asynccontextmanager
async def lifespan(_app):
    engine.start()   # background BTC/USD refresh; `engine` is defined below
    # Parse Aperture's access log once in the background so the first dashboard
    # load doesn't wait on it (`TRAFFIC` is defined below).
    threading.Thread(target=TRAFFIC.refresh, kwargs={"force": True}, daemon=True).start()
    yield
    engine.stop()


app = FastAPI(title="AurAgrid Command Center", lifespan=lifespan)
security = HTTPBasic()


# ---------------------------------------------------------------------------
# Catalog: single source of truth for endpoints, names, and (optional) prices
# ---------------------------------------------------------------------------
CATALOG = [
    # slug (path after /api/v1/), display name, tier, description
    ("batch/btc-fees", "Bitcoin Fee and Mempool Monitor", "bitcoin",
     "Fee estimates for 1-144 block targets, mempool size and minimum fee, and the last block's fee "
     "percentiles, refreshed every minute from AurAgrid's own Bitcoin Core node."),
    ("batch/btc-fees-delayed", "Bitcoin Fee and Mempool Monitor (10 min delayed)", "bitcoin",
     "The same data as btc-fees, at least 10 minutes old."),
    ("batch/ln-network", "Lightning Network Monitor", "bitcoin",
     "Lightning nodes, channels, capacity, channel-size and routing-fee percentiles, disabled share and "
     "policy churn, refreshed hourly from AurAgrid's own LND node's gossip graph."),
    ("batch/ln-network-daily", "Lightning Network Monitor (daily)", "bitcoin",
     "The same data as ln-network, as of the end of the previous UTC day."),
    ("batch/earthquakes", "Global Earthquake Activity", "hazards",
     "M2.5+ earthquakes in the last 24 h and M4.5+ in the last 7 days: counts by magnitude, largest "
     "events, tsunami-flagged and PAGER-alert events (USGS real-time feeds)."),
    ("energy/grid", "U.S. Grid Demand and Generation Mix", "energy",
     "Hourly demand, forecast, interchange and generation by fuel with renewable/carbon-free shares "
     "for the Lower 48, EIA regions and every balancing authority (U.S. EIA-930)."),
    ("batch/spr-stockpile", "U.S. Strategic Petroleum Reserve Monitor", "energy",
     "Weekly SPR and commercial crude stocks with 1/4/52-week changes, 52-week range and history "
     "(U.S. EIA Weekly Petroleum Status Report)."),
    ("batch/lng-supply-chain", "U.S. LNG Export Monitor", "energy",
     "Monthly U.S. LNG exports by terminal and destination country, export prices, and month/year "
     "changes (U.S. EIA; about three months' publication lag)."),
    ("batch/maritime-weather", "Marine Weather Observations", "maritime",
     "Latest wind, gust, wave height/period/direction, pressure and air/sea temperature from "
     "~850 NOAA NDBC buoys and coastal stations worldwide (updated continuously)."),
]


BATCH_PREFIX = os.environ.get("AURAGRID_BATCH_PREFIX", "batch")
if BATCH_PREFIX != "batch":
    CATALOG = [(slug.replace("batch/", BATCH_PREFIX + "/", 1), name, tier, desc)
               for slug, name, tier, desc in CATALOG]

APERTURE_CONFIG = os.environ.get("AURAGRID_APERTURE_CONFIG", "aperture.yaml")
PRICING_CONFIG = os.environ.get("AURAGRID_PRICING_CONFIG", "pricing.yaml")
PLANS = load_plans(PRICING_CONFIG)
PLAN_ROUTER = PlanRouter(PLANS, feed_route_prefix=BATCH_PREFIX)
ACCESS = load_access(PRICING_CONFIG)
# Endpoints from the previous catalog: 410 Gone (no reliable free source, or terms that
# don't allow redistribution; see RESEARCH.md). Listed in pricing.yaml access.retired.
RETIRED = {feed_path(k, BATCH_PREFIX)[len("/api/v1/"):] for k in ACCESS.retired} | {"telemetry"}


def feed_key(slug: str) -> str:
    return normalize_path("/api/v1/" + slug)


# Only feeds listed in pricing.yaml `access:` are offered; the rest have no data source yet.
OFFERED = [entry for entry in CATALOG if feed_key(entry[0]) in ACCESS.offered]
PAID_CATALOG = [entry for entry in OFFERED if feed_key(entry[0]) in ACCESS.paid]
# Free sample of each paid feed: its delayed/daily copy (real data, true as_of), when offered free.
SAMPLE_PATHS = {d["source"]: feed_path(k, BATCH_PREFIX) for k, d in jobs.DERIVED.items()
                if d["source"] in ACCESS.paid and k in ACCESS.free}


def sample_url(key: str):
    return SAMPLE_PATHS.get(key)

# Every offered feed is served from the snapshot store written by fetcher.py.
STORE = Store()
jobs.register_store_feeds(STORE, {feed_key(slug) for slug, _, _, _ in OFFERED})


def feed_live(key: str) -> bool:
    """Live = a stored snapshot exists and is recent enough to be served."""
    provider = feeds.get(key)
    if provider is None:
        return False
    try:
        provider()
        return True
    except Exception:
        return False
_unknown = ACCESS.offered - {feed_key(slug) for slug, _, _, _ in CATALOG}
if _unknown:
    log.error("pricing.yaml access lists feeds that are not in the catalog: %s", ", ".join(sorted(_unknown)))
_catalog_slugs = {slug for slug, _, _, _ in CATALOG}
for _plan in PLANS:
    for _feed in _plan.feeds:
        if "%s/%s" % (BATCH_PREFIX, _feed) not in _catalog_slugs:
            log.error("Plan %s lists feed %r, which is not a %s/ catalog feed", _plan.name, _feed, BATCH_PREFIX)


def load_aperture_services(path: str) -> list:
    try:
        import yaml  # PyYAML
        with open(path) as fh:
            return (yaml.safe_load(fh) or {}).get("services") or []
    except Exception as exc:
        log.warning("Could not read %s (%s): prices will be missing from the manifest", path, exc)
        return []


def build_pricing(services: list):
    """Match each catalog endpoint to the first Aperture rule covering its path.

    Returns (pricing by slug, catalog paths no rule covers, rules covering no catalog path).
    """
    rules = []
    for svc in services:
        try:
            if svc.get("pathregexp"):
                rules.append((svc, re.compile(svc["pathregexp"])))
        except re.error:
            log.warning("Invalid pathregexp in Aperture service %r", svc.get("name"))

    pricing, unmatched = {}, []
    for slug, _, _, _ in PAID_CATALOG:   # free feeds have no paid rule by design
        path = "/api/v1/" + slug
        hit = next((svc for svc, rx in rules if rx.match(path)), None)
        if hit is None:
            unmatched.append(path)
            continue
        info = {"aperture_service": hit.get("name")}
        if hit.get("price") is not None:
            info["price_sats"] = int(hit["price"])
        if hit.get("timeout"):
            info["token_ttl_seconds"] = int(hit["timeout"])
        pricing[slug] = info

    for plan in PLANS:
        for path in plan.paths():
            hit = next((svc for svc, rx in rules if rx.match(path)), None)
            if hit is None or hit.get("name") != plan.name:
                unmatched.append("%s (plan %s)" % (path, plan.name))

    paths = ["/api/v1/" + slug for slug, _, _, _ in OFFERED]
    paths += [p for plan in PLANS for p in plan.paths()]
    paths += ["/buy"] + ["/api/v1/" + slug for slug in RETIRED]
    dead = [svc.get("name") for svc, rx in rules if not any(rx.match(p) for p in paths)]
    return pricing, unmatched, dead


engine = PriceEngine(APERTURE_CONFIG, PRICING_CONFIG, degraded=lambda key: jobs.feed_degraded(STORE, key))
ledger = Ledger(os.environ.get("AURAGRID_LEDGER_DB", "ledger.db"))

# Middleware order matters (last added = outermost):
#   attestation -> ledger -> plan rewrite -> routes
# so the ledger and the signature both see the path the client paid for.
app.add_middleware(PlanRewriteMiddleware, router=PLAN_ROUTER)
app.add_middleware(L402LedgerMiddleware, ledger=ledger, normalize=normalize_path,
                   classify=PLAN_ROUTER.kind_for_path)
if not GATEWAY_SECRET:
    log.warning("AURAGRID_GATEWAY_SECRET not set: paid routes only check for an L402 header. "
                "Keep this port on loopback, or anyone reaching it skips the paywall.")

# v2 response attestation (added last = outermost, so it signs the final bytes of /api/v1/* 200s).
_attest_macaroon = os.environ.get("AURAGRID_ATTEST_MACAROON")  # path to a message:write-only macaroon
SIGNING_ENABLED = bool(_attest_macaroon)
if _attest_macaroon:
    with open(_attest_macaroon, "rb") as _fh:
        _attest_macaroon_hex = _fh.read().hex()
    app.add_middleware(
        AttestationMiddleware,
        sign=lnd_rest_signer(os.environ["LND_REST_URL"], os.environ["LND_TLS_CERT"], _attest_macaroon_hex),
    )
else:
    log.warning("AURAGRID_ATTEST_MACAROON not set: responses will NOT carry X-AurAgrid-Signature-V2")
# Outermost: compress responses (a full marine snapshot is ~420 KB raw, ~40 KB gzipped), which
# keeps outbound traffic far inside the OCI free allowance. Attestation signs the uncompressed body.
app.add_middleware(GZipMiddleware, minimum_size=1024)
PRICING, _unmatched, _dead_rules = build_pricing(load_aperture_services(APERTURE_CONFIG))
if _unmatched:
    log.warning("Catalog paths NOT covered by any Aperture rule (served unpaid or not at all): %s",
                ", ".join(_unmatched))
if _dead_rules:
    log.warning("Aperture rules that match no catalog path: %s", ", ".join(map(str, _dead_rules)))

PLAN_PRICES = {}   # plan name -> static sats from aperture.yaml
for _svc in load_aperture_services(APERTURE_CONFIG):
    if any(_svc.get("name") == _plan.name for _plan in PLANS) and _svc.get("price"):
        PLAN_PRICES[_svc["name"]] = int(_svc["price"])

# price (sats) -> names charging that price, for revenue attribution
PRICE_INDEX: dict = {}
for _slug, _name, _, _ in CATALOG:
    _price = PRICING.get(_slug, {}).get("price_sats")
    if _price:
        PRICE_INDEX.setdefault(_price, []).append(_name)
for _plan in PLANS:
    if not _plan.dynamic and PLAN_PRICES.get(_plan.name):   # pegged prices move; only static ones identify
        PRICE_INDEX.setdefault(PLAN_PRICES[_plan.name], []).append(_plan.display_name())

# Shown in the revenue breakdown only (not advertised as a data endpoint).
EXTRA_SERVICES = []   # nothing is sold outside the catalog and plans
UNATTRIBUTED = "Unattributed"

BY_SLUG = {slug: name for slug, name, _, _ in CATALOG}
PLAN_KIND_BY_NAME = {plan.display_name(): plan.kind for plan in PLANS}
CATALOG_NAMES = {name for _, name, _, _ in CATALOG}
ALL_NAMES = [name for _, name, _, _ in CATALOG] + list(PLAN_KIND_BY_NAME) + EXTRA_SERVICES


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Longest names first so a short name can never swallow a longer one.
LEGACY_MATCH = sorted(((n, _norm(n)) for n in ALL_NAMES), key=lambda p: -len(p[1]))


def attribute(memo: str, sats: int = 0) -> str:
    norm_memo = _norm(memo)
    for name, norm_name in LEGACY_MATCH:
        if norm_name in norm_memo:
            return name
    names = PRICE_INDEX.get(sats, [])
    if len(names) == 1:
        return names[0]
    if len(names) > 1:
        return "%d-sat tier (%d services)" % (sats, len(names))
    return UNATTRIBUTED


NAME_BY_ENDPOINT = {normalize_path("/api/v1/" + slug): name for slug, name, _, _ in CATALOG}
for _plan in PLANS:
    for _path in _plan.paths():
        NAME_BY_ENDPOINT[normalize_path(_path)] = _plan.display_name()


def plan_kind_for(service: str, ledger_plan=None) -> str:
    """hourly | pass_30d | bundle | unattributed, for the revenue-by-plan split."""
    if ledger_plan:
        return ledger_plan
    if service in PLAN_KIND_BY_NAME:
        return PLAN_KIND_BY_NAME[service]
    return HOURLY if service in CATALOG_NAMES else UNATTRIBUTED.lower()


def service_for_endpoint(endpoint: str) -> str:
    """Map a ledger endpoint (normalized path) to a catalog display name."""
    best = None
    for key, name in NAME_BY_ENDPOINT.items():
        if endpoint == key or endpoint.startswith(key + "/"):
            if best is None or len(key) > len(best[0]):
                best = (key, name)
    return best[1] if best else endpoint


def attribute_invoice(inv: dict, sats: int, by_hash: dict):
    """Return (service name, 'ledger' | 'fallback')."""
    endpoint = by_hash.get(hash_hex(inv.get("r_hash")))
    if endpoint:
        return service_for_endpoint(endpoint), "ledger"
    return attribute(inv.get("memo", ""), sats), "fallback"


# ---------------------------------------------------------------------------
# Operator auth (fail closed)
# ---------------------------------------------------------------------------
def require_admin(creds: HTTPBasicCredentials = Depends(security)) -> None:
    if not ADMIN_USER or not ADMIN_PASSWORD:
        raise HTTPException(status_code=503, detail="Operator auth is not configured")
    user_ok = secrets.compare_digest(creds.username.encode(), ADMIN_USER.encode())
    pass_ok = secrets.compare_digest(creds.password.encode(), ADMIN_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(status_code=401, detail="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="AurAgrid"'})


def require_gateway(request: Request) -> None:
    """Paid routes: only serve requests Aperture has already authorized."""
    if GATEWAY_SECRET:
        sent = request.headers.get(GATEWAY_HEADER, "")
        if not secrets.compare_digest(sent.encode(), GATEWAY_SECRET.encode()):
            raise HTTPException(status_code=403, detail="Paid routes are only served through the L402 gateway")
    elif not token_payment_hash(request.headers.get("authorization", "")):
        raise HTTPException(status_code=401, detail="L402 token required")


# ---------------------------------------------------------------------------
# LND snapshot cache: one fetch shared by all dashboard endpoints
# ---------------------------------------------------------------------------
CACHE_TTL_SECONDS = 5.0
_cache = {"t": 0.0, "invoices": [], "node": {}, "total": 0}
_lock = threading.Lock()


def snapshot():
    with _lock:
        if time.monotonic() - _cache["t"] > CACHE_TTL_SECONDS:
            try:
                page = getattr(lnd, "get_invoices_page", None)   # test doubles may lack it
                if page:
                    _cache["invoices"], _cache["total"] = page()
                else:
                    _cache["invoices"] = lnd.get_invoices()
                    _cache["total"] = len(_cache["invoices"])
                _cache["node"] = lnd.get_mainnet_node_stats()
            except Exception as exc:
                log.exception("LND fetch failed")
                raise HTTPException(status_code=502, detail="LND unavailable") from exc
            _cache["t"] = time.monotonic()
        return _cache["invoices"], _cache["node"]


def is_settled(inv: dict) -> bool:
    return bool(inv.get("settled")) or str(inv.get("state", "")).upper() == "SETTLED"


def paid_sats(inv: dict) -> int:
    paid = int(inv.get("amt_paid_sat") or 0)
    return paid or int(inv.get("value") or 0)


# ---------------------------------------------------------------------------
# Operator API
# ---------------------------------------------------------------------------
@app.get("/api/proxy/stats", dependencies=[Depends(require_admin)], include_in_schema=False)
def proxy_stats():
    invoices, node = snapshot()

    by_hash = ledger.endpoints_by_hash()
    plan_by_hash = ledger.plans_by_hash()
    usage = ledger.usage_by_endpoint()

    revenue = {name: 0 for name in ALL_NAMES}
    revenue[UNATTRIBUTED] = 0
    by_plan = {kind: {"revenue_sats": 0, "settled_invoices": 0} for kind in (HOURLY, "pass_30d", "bundle")}
    settled_count = from_ledger = 0
    for inv in invoices:
        if not is_settled(inv):
            continue  # unpaid / expired invoices are not revenue
        settled_count += 1
        sats = paid_sats(inv)
        service, how = attribute_invoice(inv, sats, by_hash)
        from_ledger += how == "ledger"
        revenue[service] = revenue.get(service, 0) + sats
        kind = plan_kind_for(service, plan_by_hash.get(hash_hex(inv.get("r_hash"))))
        row = by_plan.setdefault(kind, {"revenue_sats": 0, "settled_invoices": 0})
        row["revenue_sats"] += sats
        row["settled_invoices"] += 1

    calls = {}
    for endpoint, u in usage.items():
        name = service_for_endpoint(endpoint)
        calls[name] = calls.get(name, 0) + u["calls"]
        revenue.setdefault(name, 0)

    total_count = len(invoices)
    breakdown = sorted(
        ({"service_name": n, "total_revenue_sats": r, "calls": calls.get(n, 0)}
         for n, r in revenue.items()),
        key=lambda row: (-row["total_revenue_sats"], -row["calls"]),
    )

    return {
        "total_revenue_sats": sum(revenue.values()),
        "settled_count": settled_count,
        # All invoices ever issued (LND's index), not just the newest batch analysed above.
        "transaction_count": _cache.get("total") or total_count,
        "transactions_analysed": total_count,
        "success_rate": round(settled_count / total_count * 100, 1) if total_count else 0.0,
        # Only real values from the node; None renders as "n/a" in the UI.
        "active_channels": node.get("active_channels"),
        "total_capacity_sats": node.get("total_capacity"),
        "forwarding_fee_apr": node.get("forwarding_fee_apr"),
        "htlc_success_rate": node.get("htlc_success_rate"),
        "service_breakdown": breakdown,
        "revenue_by_plan": by_plan,
        "usage_by_plan": ledger.usage_by_plan(),
        # Diagnostics: if tokens stays 0 while served_without_token grows, Aperture
        # is not forwarding the Authorization header to this backend.
        "ledger": {
            "tokens": sum(u["tokens"] for u in usage.values()),
            "calls": sum(u["calls"] for u in usage.values()),
            "served_without_token": ledger.served_without_token,
            "settled_attributed_by_ledger": from_ledger,
            "settled_attributed_by_fallback": settled_count - from_ledger,
        },
    }


@app.get("/api/proxy/info", dependencies=[Depends(require_admin)], include_in_schema=False)
def proxy_info():
    return snapshot()[1]


@app.get("/api/proxy/transactions", dependencies=[Depends(require_admin)], include_in_schema=False)
def proxy_transactions(limit: int = Query(200, ge=1, le=1000)):
    invoices, _ = snapshot()
    by_hash = ledger.endpoints_by_hash()
    plan_by_hash = ledger.plans_by_hash()
    newest = sorted(invoices, key=lambda i: int(i.get("creation_date") or 0), reverse=True)[:limit]
    rows = []
    for inv in newest:
        amount = int(inv.get("value") or 0)
        service = attribute_invoice(inv, amount, by_hash)[0]
        rows.append({
            "payment_hash": inv.get("r_hash"),
            "amount_sat": amount,
            "memo": inv.get("memo") or "L402 Telemetry Payload",
            "service": service,
            "plan": plan_kind_for(service, plan_by_hash.get(hash_hex(inv.get("r_hash")))),
            "state": "settled" if is_settled(inv) else "unpaid",
            "created_at": inv.get("creation_date"),
        })
    return {"transactions": rows}


@app.get("/api/proxy/services", dependencies=[Depends(require_admin)], include_in_schema=False)
def proxy_services():
    return {"services": _catalog_entries()}


# ---------------------------------------------------------------------------
# Operator monitoring: health and public traffic (ops_monitor.py)
# ---------------------------------------------------------------------------
def _route_sets():
    paid = {normalize_path("/api/v1/" + slug) for slug, *_ in PAID_CATALOG}
    paid |= {normalize_path(p) for plan in PLANS for p in plan.paths()}
    free = {normalize_path("/api/v1/" + slug) for slug, *_ in OFFERED} - paid
    return paid, free


PAID_PATHS, FREE_PATHS = _route_sets()


def classify_path(path: str) -> str:
    """Traffic category for one request path (see ops_monitor.CATEGORIES)."""
    norm = normalize_path(path) if path.startswith("/api/v1/") else None
    if norm in PAID_PATHS:
        return "paid"
    if norm in FREE_PATHS or (norm and norm in derived.ENDPOINTS):
        return "free"
    if path.startswith("/api/v1/price/"):
        return "quote"
    if path in ops_monitor.DISCOVERY:
        return "discovery"
    return "noise"


def _own_ips() -> set:
    host = re.sub(r"^https?://", "", PUBLIC_URL).split("/")[0].split(":")[0]
    return {host} if re.fullmatch(r"[0-9.]+", host or "") else set()


TRAFFIC = ops_monitor.TrafficLog(ops_monitor.APERTURE_LOG, classify_path, self_ips=_own_ips(),
                                 sample_paths=set(SAMPLE_PATHS.values()))
JOBS = {job.key: job for job in jobs.all_jobs()}


def _feed_health() -> list:
    now = time.time()
    rows = []
    for key, job in JOBS.items():
        st = STORE.status(key) or {}
        last_ok = st.get("last_success")
        newest = STORE.latest_retrieved_at(key)
        age = now - newest if newest else None   # newest stored data, as StoreFeed measures it
        # The fetcher, not this process, holds feed credentials (EIA_API_KEY), so a
        # feed it has ever attempted counts as enabled even if job.enabled() is False here.
        enabled = job.enabled() or bool(st.get("last_attempt"))
        rows.append({
            "feed": key,
            "enabled": enabled,
            "interval_s": job.interval,
            "stale_after_s": job.stale_after,
            "last_success": last_ok,
            "last_success_age_s": round(now - last_ok) if last_ok else None,
            "last_attempt": st.get("last_attempt"),
            "age_s": round(age) if age is not None else None,
            "failures": st.get("consecutive_failures") or 0,
            "last_error": st.get("last_error"),
            "ok": enabled and age is not None and age <= job.stale_after,
        })
    return rows


@app.get("/api/proxy/health", dependencies=[Depends(require_admin)], include_in_schema=False)
def proxy_health():
    services = ops_monitor.service_status()
    feeds_ = _feed_health()
    cert = ops_monitor.cert_status()
    disk = ops_monitor.disk_status()
    db_mb = round(STORE.size_bytes() / 2**20, 1)
    alerts = [s["label"] + " is " + s["state"] for s in services if not s["ok"]]
    # A free feed whose fetches succeed but bring nothing new is the source lagging
    # (e.g. EIA not publishing): a notice, not an alert. Failures or a stale paid feed alert.
    paid_keys = {feed_key(slug) for slug, *_ in PAID_CATALOG}
    notices = []
    for f in feeds_:
        if not f["enabled"] or f["ok"]:
            continue
        age = "never" if f["age_s"] is None else ("%.1fh" % (f["age_s"] / 3600) if f["age_s"] >= 5400
                                                 else "%dm" % (f["age_s"] // 60))
        if f["failures"] or f["feed"] in paid_keys or f["age_s"] is None:
            alerts.append("Feed %s: newest data %s old, %d consecutive failures" % (f["feed"], age, f["failures"]))
        else:
            notices.append("Feed %s: the source has published nothing new for %s (fetches succeed; "
                           "served marked stale)" % (f["feed"], age))
    if not cert.get("ok"):
        alerts.append("TLS certificate: " + (cert.get("error") or "%.1f days left" % cert["days_left"]))
    if not disk["ok"]:
        alerts.append("Disk %.0f%% full" % disk["used_pct"])
    if not SIGNING_ENABLED:
        alerts.append("Response signing is off")
    selfcheck = ops_monitor.selfcheck_status()
    if selfcheck.get("failures"):
        alerts += ["Weekly self-check: " + f for f in selfcheck["failures"]]
    elif selfcheck.get("last_run") and selfcheck["age_days"] > 8:
        alerts.append("Weekly self-check has not run for %.0f days" % selfcheck["age_days"])
    elif selfcheck.get("error"):
        notices.append("Weekly self-check: " + selfcheck["error"])
    return {
        "checked_at": time.time(),
        "ok": not alerts,
        "alerts": alerts,
        "notices": notices,
        "services": services,
        "feeds": feeds_,
        "cert": cert,
        "disk": disk,
        "selfcheck": selfcheck,
        "feeds_db_mb": db_mb,
        "signing_enabled": SIGNING_ENABLED,
        "public_url": PUBLIC_URL or None,
    }


@app.get("/api/proxy/traffic", dependencies=[Depends(require_admin)], include_in_schema=False)
def proxy_traffic(days: int = Query(7, ge=1, le=14)):
    summary = TRAFFIC.summary(days)
    cutoff = time.time() - days * 86400
    since = datetime.fromtimestamp(cutoff, timezone.utc).date().isoformat()
    summary["totals"]["stale_refusals"] = ledger.counter_total("stale_refusal", since)
    try:
        invoices, _ = snapshot()
    except HTTPException:
        invoices = None
    if invoices is not None:
        recent = [i for i in invoices if int(i.get("creation_date") or 0) >= cutoff]
        summary["funnel"] = {
            "paid_requests": summary["totals"]["paid"],
            "invoices_issued": len(recent),
            "invoices_settled": sum(1 for i in recent if is_settled(i)),
            "sats_settled": sum(paid_sats(i) for i in recent if is_settled(i)),
        }
    else:
        summary["funnel"] = None
    return summary


# ---------------------------------------------------------------------------
# Agent manifest (public)
# ---------------------------------------------------------------------------
CONTACT = os.environ.get("AURAGRID_CONTACT", "").strip()   # owner-supplied; omitted when unset
STARTED_AT = time.time()


def _source_key(key: str) -> str:
    """The fetched feed behind a key (a delayed/daily copy reads its source's snapshots)."""
    return jobs.DERIVED.get(key, {}).get("source", key)


def _freshness(key: str) -> dict:
    """Age of the newest stored data behind an endpoint, against its stale limit."""
    if key in derived.ENDPOINTS:   # a derived endpoint is as fresh as its stalest input
        parts = [_freshness(k) for k in derived.ENDPOINTS[key]["inputs"]]
        worst = max(parts, key=lambda p: (p["stale"], p["age_seconds"] or 10**9))
        return {**worst, "inputs": derived.ENDPOINTS[key]["inputs"]}
    src = _source_key(key)
    newest = STORE.latest_retrieved_at(src)
    stale_after = jobs.LIMITS.get(key, (None,))[0]
    age = round(time.time() - newest) if newest else None
    return {"newest_data_retrieved_at": datetime.fromtimestamp(newest, timezone.utc).isoformat(timespec="seconds")
            if newest else None,
            "age_seconds": age, "stale_after_seconds": stale_after,
            "stale": age is None or (stale_after is not None and age > stale_after)}


def _update_interval(key: str):
    if key in derived.ENDPOINTS:
        return max(_update_interval(k) or 0 for k in derived.ENDPOINTS[key]["inputs"]) or None
    copy = jobs.DERIVED.get(key, {})
    if copy.get("daily"):
        return 86400
    job = JOBS.get(_source_key(key))
    return job.interval if job else None


def _usd(sats):
    btc, _age, state = engine.btc_state()
    return round(sats / 1e8 * btc, 4) if btc and sats is not None else None


def _catalog_entries() -> list[dict]:
    entries = []
    for slug, name, tier, desc in OFFERED:
        key = feed_key(slug)
        free = key in ACCESS.free
        entry = {"path": "/api/v1/" + slug, "name": name, "tier": tier, "description": desc,
                 "access": "free" if free else "paid", "auth": "none" if free else "L402",
                 "status": "live" if feed_live(key) else "coming_soon",
                 "method": "measured",
                 "update_interval_seconds": _update_interval(key),
                 "stale_after_seconds": jobs.LIMITS.get(key, (None,))[0],
                 **{("source_" + k if k in ("url", "terms_url") else k): v
                    for k, v in feeds.source_info(_source_key(key)).items()}}
        copy = jobs.DERIVED.get(key)
        if copy:
            entry["copy_of"] = feed_path(copy["source"], BATCH_PREFIX)
            entry["delay"] = "previous UTC day" if copy.get("daily") else "%d minutes" % (copy["delay"] // 60)
        if free:
            entry["rate_limit"] = "%d requests/minute per IP" % ACCESS.requests_per_minute
            entries.append(entry)
            continue
        info = PRICING.get(slug, {})
        if info.get("price_sats") is not None:
            entry["price_sats"] = info["price_sats"]
        if info.get("token_ttl_seconds"):
            entry["token_ttl_seconds"] = info["token_ttl_seconds"]
        quote = engine.quote("/api/v1/" + slug)
        if quote:
            entry["price_sats"] = quote["price_sats"]
            entry["pricing"] = quote["pricing"]
            if quote.get("degraded"):
                entry["status"] = "stale"   # past its limit: 503, no invoice, until fresh
            if "usd_price" in quote:
                entry["usd_price"] = quote["usd_price"]
            entry["quote_url"] = "/api/v1/price/" + slug
            if quote.get("token_ttl_seconds"):
                entry["token_ttl_seconds"] = quote["token_ttl_seconds"]
        entry["price_usd_approx"] = _usd(entry.get("price_sats"))
        if sample_url(key):
            entry["sample_url"] = sample_url(key)
        entries.append(entry)
    for key, meta in derived.ENDPOINTS.items():
        fr = _freshness(key)
        entry = {"path": "/api/v1/" + key, "name": meta["name"], "tier": meta["tier"],
                 "description": meta["description"], "access": "free", "auth": "none",
                 "status": "preview", "method": "derived", "inputs": [feed_path(k, BATCH_PREFIX) for k in meta["inputs"]],
                 "update_interval_seconds": _update_interval(key), "stale_after_seconds": fr["stale_after_seconds"],
                 "source": "Derived by AurAgrid (see provenance.derivation)", "license": meta["license"],
                 "rate_limit": "%d requests/minute per IP" % ACCESS.requests_per_minute,
                 "proposed_price_usd": meta["proposed_price_usd"],
                 "pricing_note": "Free preview; a price is proposed and awaits the owner's approval."}
        if meta.get("parameters"):
            entry["parameters"] = meta["parameters"]
        entries.append(entry)
    return entries


def _plan_entries() -> list[dict]:
    entries = []
    for plan in PLANS:
        entry = {"name": plan.name, "title": plan.display_name(), "kind": plan.kind,
                 "paths": plan.paths(), "token_ttl_seconds": plan.timeout, "auth": "L402",
                 "pricing": "usd_pegged" if plan.dynamic else "static"}
        quote = engine.quote(plan.paths()[0])
        if quote:
            entry["price_sats"] = quote["price_sats"]
            entry["price_usd_approx"] = _usd(quote["price_sats"])
            entry["quote_url"] = "/api/v1/price/" + plan.paths()[0][len("/api/v1/"):]
            if "usd_price" in quote:
                entry["usd_price"] = quote["usd_price"]
        entry["samples"] = [sample_url(f) for f in plan.feeds if sample_url(f)]
        entries.append(entry)
    return entries


def _links(base: str) -> dict:
    links = {"manifest": base + "/agent-manifest.json", "openapi": base + "/openapi.json",
             "llms_txt": base + "/llms.txt", "l402_services": base + "/.well-known/l402-services",
             "status": base + "/status.json", "terms": base + "/terms", "checkout": base + "/buy"}
    return links


def _status_payload() -> dict:
    feeds_ = []
    for e in _catalog_entries():
        fr = _freshness(feed_key(e["path"][len("/api/v1/"):]))
        feeds_.append({"path": e["path"], "name": e["name"], "access": e["access"], "status": e["status"],
                       **fr, "update_interval_seconds": e["update_interval_seconds"]})
    paid_stale = [f["path"] for f in feeds_ if f["access"] == "paid" and f["stale"]]
    return {
        "service": "AurAgrid",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api_up_since": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat(timespec="seconds"),
        "api_uptime_seconds": round(time.time() - STARTED_AT),
        "overall": "degraded" if paid_stale else "ok",
        "paid_feeds_refusing_while_stale": paid_stale,
        "feeds": feeds_,
        "note": "Free feeds are served marked stale; paid feeds past their limit answer 503 and issue no invoice.",
    }


_status_cache = {"t": 0.0, "v": None}
STATUS_TTL_SECONDS = 15.0


def _status_cached() -> dict:
    """Public status, recomputed at most every 15 s (it reads every feed's newest snapshot)."""
    if _status_cache["v"] is None or time.monotonic() - _status_cache["t"] > STATUS_TTL_SECONDS:
        _status_cache["v"], _status_cache["t"] = _status_payload(), time.monotonic()
    return _status_cache["v"]


@app.get("/status.json")
def status_json():
    """Freshness of every feed and the API's uptime (honest, computed at most every 15 s)."""
    return JSONResponse(_status_cached(), headers={"Cache-Control": "no-store"})


@app.get("/status", response_class=HTMLResponse, include_in_schema=False)
def status_page():
    st = _status_cached()
    esc = lambda v: str(v).replace("&", "&amp;").replace("<", "&lt;")   # noqa: E731
    def age(sec):
        return "never" if sec is None else ("%dm" % (sec // 60) if sec < 5400 else "%.1fh" % (sec / 3600))
    rows = "".join(
        "<tr><td><code>%s</code></td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            esc(f["path"]), esc(f["access"]), "stale" if f["stale"] else "fresh", age(f["age_seconds"]),
            age(f["stale_after_seconds"]))
        for f in st["feeds"])
    html = ("<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,"
            "initial-scale=1'><title>AurAgrid status</title><style>body{font:15px system-ui,sans-serif;margin:24px;"
            "max-width:860px}table{border-collapse:collapse;width:100%%}td,th{text-align:left;padding:6px 8px;"
            "border-bottom:1px solid #ddd}code{font-size:13px}</style></head><body><h1>AurAgrid status</h1>"
            "<p>Overall: <b>%s</b> &middot; API up since %s &middot; checked %s UTC</p>"
            "<table><tr><th>Endpoint</th><th>Access</th><th>Data</th><th>Newest data age</th><th>Stale after</th></tr>"
            "%s</table><p>%s</p><p>Machine-readable: <a href=/status.json>/status.json</a> &middot; "
            "<a href=/agent-manifest.json>manifest</a> &middot; <a href=/openapi.json>OpenAPI</a></p></body></html>"
            ) % (esc(st["overall"]), esc(st["api_up_since"]), esc(st["checked_at"]), rows, esc(st["note"]))
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


ROBOTS_TXT = """# AurAgrid: discovery files and docs are open to crawlers. Data endpoints are for API
# clients, not crawlers (paid ones answer 402 and would only create unpaid invoices).
User-agent: *
Allow: /llms.txt
Allow: /agent-manifest.json
Allow: /openapi.json
Allow: /.well-known/
Allow: /status
Allow: /terms
Allow: /api/v1/price/
Disallow: /api/v1/
"""


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt():
    return ROBOTS_TXT


@app.get("/.well-known/l402-services", include_in_schema=False)
def l402_services(request: Request):
    """Seller discovery document read by 402 Index (no published schema; fields are self-describing)."""
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    services = []
    for e in _catalog_entries():
        if e["access"] != "paid":
            continue
        services.append({"url": base + e["path"], "method": "GET", "protocol": "L402", "name": e["name"],
                         "description": e["description"], "price_sats": e.get("price_sats"),
                         "price_usd": e.get("usd_price", e.get("price_usd_approx")),
                         "token_ttl_seconds": e.get("token_ttl_seconds"), "category": "data",
                         "request_schema": None,   # GET with no body or parameters
                         "quote_url": base + e["quote_url"] if e.get("quote_url") else None,
                         "sample_url": base + e["sample_url"] if e.get("sample_url") else None,
                         "status": e["status"]})
    for p in _plan_entries():
        for path in p["paths"]:
            services.append({"url": base + path, "method": "GET", "protocol": "L402", "name": p["title"],
                             "description": "%d-day token for: %s" % (p["token_ttl_seconds"] // 86400,
                                                                      ", ".join(p["paths"])),
                             "price_sats": p.get("price_sats"), "price_usd": p.get("price_usd_approx"),
                             "token_ttl_seconds": p["token_ttl_seconds"], "category": "data",
                             "request_schema": None, "samples": [base + x for x in p["samples"]]})
    doc = {"provider": "AurAgrid", "services": services, "links": _links(base),
           "signing_pubkey": SIGNING_PUBKEY if SIGNING_ENABLED else None}
    if CONTACT:
        doc["contact"] = CONTACT
    return doc


@app.get("/.well-known/ai-plugin.json")
@app.get("/agent-manifest.json")
async def get_agent_manifest(request: Request):
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    entries = _catalog_entries()
    signature = {
        "scheme": "auragrid_attest_v2",
        "header": attestation.SIG_HEADER_V2,
        "timestamp_header": attestation.TIMESTAMP_HEADER,
        "nonce_request_header": attestation.NONCE_HEADER,
        "message_lines": ["auragrid_attest_v2", "<METHOD>", "<path?query as sent>", "<sha256 hex of body>",
                          "<unix timestamp>", "<nonce or ->"],
        "message_join": "newline",
        "format": "LND signmessage (zbase32 recoverable ECDSA); recover the key and compare it to pubkey",
        "enabled": SIGNING_ENABLED,
    }
    if SIGNING_PUBKEY:
        signature["pubkey"] = SIGNING_PUBKEY
    return {
        "schema_version": "v1",
        "name_for_human": "AurAgrid L402 Oracle",
        "name_for_model": "auragrid_oracle",
        "description_for_model": (
            "AurAgrid serves %d data endpoints computed from its own Bitcoin and Lightning nodes or "
            "from U.S. public-domain sources (EIA, NOAA, USGS); each response carries provenance. Endpoints "
            "with access 'free' need no payment and are rate-limited per IP (see terms_url); only "
            "use endpoints whose status is 'live'. Endpoints with access 'paid' use L402 Lightning "
            "payments. On a 402 "
            "Payment Required, extract the invoice and macaroon from the WWW-Authenticate "
            "header, pay the invoice with a Lightning node, and replay the request with "
            "'Authorization: L402 <macaroon>:<preimage>'. Prices may be USD-pegged and "
            "change with the BTC price: GET the endpoint's quote_url right before paying and "
            "accept an invoice within the quote's tolerance_pct. A paid token stays valid for the "
            "endpoint's token_ttl_seconds, so reuse it instead of paying again. For repeated use, "
            "a plan (30-day pass or multi-feed bundle) sells one token that opens its paths for "
            "token_ttl_seconds. Every paid endpoint has a free sample (sample_url: its delayed or daily "
            "copy, real data with its true as_of). A paid endpoint whose data is past its stale limit "
            "answers 503 with stale=true and its as_of, and issues no invoice; check status_url first."
            % len(entries)
            + (" Endpoints with method 'derived' are computed by AurAgrid from its stored feeds; "
               "provenance.derivation gives the formula and provenance.inputs each input's as_of.")
            + (" Successful /api/v1/ data responses carry X-AurAgrid-Signature-V2 and X-AurAgrid-Timestamp "
               "headers (send a random X-AurAgrid-Nonce to bind the signature to your request); verify them "
               "as described in `signature` against the published pubkey, pinned out-of-band."
               if SIGNING_ENABLED else "")
        ),
        "api": {"type": "openapi", "url": base + "/openapi.json"},
        "terms_url": base + "/terms",
        "status_url": base + "/status.json",
        "links": _links(base),
        **({"contact": CONTACT} if CONTACT else {}),
        "endpoints_count": len(entries),
        "endpoints": entries,
        "plans": _plan_entries(),
        "checkout_url": base + "/buy",
        "payment_protocol": "L402",
        "signature": signature,
    }


@app.get("/api/v1/price/{slug:path}")
def get_endpoint_quote(slug: str):
    """Public, free quote for an endpoint (same price the Aperture pricer charges)."""
    quote = engine.quote("/api/v1/" + slug)
    if quote is None:
        raise HTTPException(status_code=404, detail="Endpoint not priced")
    plan = PLAN_ROUTER.plan_for_path("/api/v1/" + slug)
    quote["plan"] = plan.kind if plan else HOURLY
    sample = sample_url(engine.feed_key("/api/v1/" + slug))
    if sample:
        quote["sample_url"] = sample
    if quote.get("degraded"):
        quote["note"] = ("This feed's data is past its freshness limit: requests return 503 with no invoice "
                         "until fresh data arrives. The free sample stays available.")
    return quote


# ---------------------------------------------------------------------------
# Retired endpoints: 410 Gone, with a pointer to what replaced them
# ---------------------------------------------------------------------------
def _gone(path: str):
    return JSONResponse(
        {"detail": "This endpoint has been retired.", "path": path,
         "reason": "No reliable free data source, or the source's terms do not allow redistribution.",
         "current_endpoints": "/agent-manifest.json"},
        status_code=410, headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/v1/sample/{slug:path}", include_in_schema=False)
@app.get("/api/v1/pro/{slug:path}", include_in_schema=False)
def retired_prefixes(request: Request, slug: str):
    return _gone(request.url.path)


# ---------------------------------------------------------------------------
# Paid feeds. Aperture sells and checks the token; passes and bundles arrive
# here rewritten by PlanRewriteMiddleware. Data comes from feeds.py providers.
# ---------------------------------------------------------------------------
def _stale_refusal(key: str, as_of):
    """503 for a paid feed past its freshness limit: no data, no charge."""
    if as_of is None:
        snap = STORE.latest(key)
        as_of = snap.as_of if snap else None
    ledger.bump("stale_refusal", datetime.now(timezone.utc).date().isoformat())
    body = {"detail": "Data is past its freshness limit; not served and not charged until fresh data arrives.",
            "feed": key, "stale": True, "as_of": as_of,
            "stale_after_seconds": jobs.LIMITS.get(key, (None,))[0], "sample_url": sample_url(key),
            "status_url": "/status.json"}
    return JSONResponse(body, status_code=503, headers={"Retry-After": "300", "Cache-Control": "no-store"})


def _feed_handler(slug: str):
    key = normalize_path("/api/v1/" + slug)
    paid = key in ACCESS.paid

    def handler(request: Request):
        try:
            data = feeds.fetch(key)
        except feeds.FeedNotConnected:
            log.error("Paid request for %s, but no provider is registered in feeds.py", key)
            raise HTTPException(status_code=503, detail="Feed not connected")
        except feeds.FeedWarmingUp:
            if paid:
                return _stale_refusal(key, None)
            raise HTTPException(status_code=503, detail="Feed warming up", headers={"Retry-After": "300"})
        except Exception:
            log.exception("Provider for %s failed", key)
            raise HTTPException(status_code=502, detail="Feed temporarily unavailable")
        data = dict(data)
        retrieved_at, stale = data.pop("fetched_at", None), bool(data.pop("stale", False))
        if paid and stale:
            # Never take payment for stale data: the pricer quotes 0 sats while a paid feed
            # is degraded, so Aperture issues no invoice, and the request ends here.
            return _stale_refusal(key, feeds.provenance(key, data, retrieved_at).get("as_of"))
        return {
            "feed": key,
            "plan": getattr(request.state, "auragrid_plan_kind", HOURLY),
            "served_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": stale,
            "degraded": stale,   # only free feeds can be served stale; paid ones answer 503
            "provenance": feeds.provenance(key, data, retrieved_at),
            "data": data,
        }

    return handler


class Provenance(BaseModel):
    source: Optional[str] = None
    url: Optional[str] = None
    terms_url: Optional[str] = None
    attribution: Optional[str] = None
    retrieved_at: Optional[str] = Field(None, description="When AurAgrid fetched this data (ISO 8601, UTC)")
    as_of: Optional[str] = Field(None, description="The data's own timestamp, as published by the source")


class FeedResponse(BaseModel):
    feed: str
    plan: str = Field(description="hourly, pass_30d or bundle: the token type that was used")
    served_at: str
    stale: bool = Field(description="True when the data is older than the feed's stale limit (free feeds only)")
    degraded: bool
    provenance: Optional[Provenance] = None
    data: dict = Field(description="The feed's payload; its fields are documented by each source")


class StaleRefusal(BaseModel):
    detail: str
    feed: str
    stale: bool = True
    as_of: Optional[str] = None
    stale_after_seconds: Optional[int] = None
    sample_url: Optional[str] = None
    status_url: str


for _slug, _name, _tier, _desc in OFFERED:
    _free = feed_key(_slug) in ACCESS.free
    _responses = {200: {"model": FeedResponse}, 503: {"description": "No data yet (warming up)"}}
    if not _free:
        _responses[503] = {"model": StaleRefusal,
                           "description": "Data past its stale limit: not served, no invoice issued"}
    app.add_api_route("/api/v1/" + _slug, _feed_handler(_slug), methods=["GET"], name=_slug, summary=_name,
                      description=_desc + (" Free, rate-limited per IP." if _free else " Requires L402."),
                      tags=["free" if _free else "paid"], responses=_responses,
                      dependencies=[] if _free else [Depends(require_gateway)])
def _retired_route(request: Request):
    return _gone(request.url.path)


for _slug in sorted(RETIRED):
    app.add_api_route("/api/v1/" + _slug, _retired_route, methods=["GET"], include_in_schema=False)
for _slug, _name, _, _ in PAID_CATALOG:
    if not feeds.get(feed_key(_slug)):
        log.error("Paid feed %s has no data provider: buyers would pay and get a 503. "
                  "Remove it from pricing.yaml access.paid or connect it.", feed_key(_slug))


# ---------------------------------------------------------------------------
# Discovery for LLM agents
# ---------------------------------------------------------------------------
TERMS_TEXT = """AurAgrid terms of use

Free tier
- Endpoints marked "free" in /agent-manifest.json need no account or payment.
- Limit: %(rpm)d requests per minute per client IP. Excess requests get HTTP 429.
  Responses change at most every few minutes (most sources update hourly, weekly
  or monthly), so cache them rather than polling.
- The free tier is offered during launch. If its terms change, we will announce
  it in /agent-manifest.json and /llms.txt at least 90 days in advance.

Data sources and attribution
- Every response names its source (e.g. U.S. Energy Information Administration,
  NOAA, ERCOT, IMF PortWatch) and keep that attribution when you republish.
- Underlying public data is available free of charge from those sources.
  No source endorses AurAgrid.

Paid endpoints
- Paid endpoints use L402 (Lightning): pay the invoice in the 402 response and
  replay the request with the token. A token works until its stated expiry.

No warranty
- Data is provided "as is", may be delayed, revised or unavailable, and is not
  investment, trading or safety advice.
"""


@app.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
def llms_txt(request: Request):
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    lines = ["# AurAgrid", "",
             "> Bitcoin, Lightning, energy, hazard and marine data: computed from AurAgrid's own Bitcoin "
             "and Lightning nodes, or from U.S. public-domain sources (EIA, NOAA, USGS). "
             "Free endpoints need no key; premium endpoints are paid per use over L402 (Lightning).", "",
             "- Manifest: %s/agent-manifest.json" % base,
             "- OpenAPI: %s/openapi.json" % base,
             "- L402 services: %s/.well-known/l402-services" % base,
             "- Status (feed freshness, uptime): %s/status.json" % base,
             "- Terms: %s/terms" % base]
    if CONTACT:
        lines.append("- Contact: %s" % CONTACT)
    if SIGNING_ENABLED and SIGNING_PUBKEY:
        lines.append("- Responses are signed (X-AurAgrid-Signature-V2) by node key %s; see the manifest's "
                     "`signature` block" % SIGNING_PUBKEY)

    def every(sec):
        if sec is None:
            return "?"
        return "%d min" % (sec // 60) if sec < 3600 else ("%d h" % (sec // 3600) if sec < 86400 else "%d day" % (sec // 86400))

    entries = _catalog_entries()
    for title, access in (("Free endpoints (rate-limited per IP)", "free"), ("Paid endpoints (L402)", "paid")):
        rows = [e for e in entries if e["access"] == access]
        if not rows:
            continue
        lines += ["", "## " + title]
        for e in rows:
            bits = ["source: %s" % e.get("source", "?"), "updates every %s" % every(e["update_interval_seconds"])]
            if e.get("license"):
                bits.append("licence: %s" % e["license"])
            if access == "paid":
                price = "%s sats" % e.get("price_sats")
                if e.get("price_usd_approx") is not None:
                    price += " (~$%.4f)" % e["price_usd_approx"]
                if e.get("token_ttl_seconds"):
                    price += " per %s token" % every(e["token_ttl_seconds"])
                bits.insert(0, price)
                if e.get("sample_url"):
                    bits.append("free sample: %s%s" % (base, e["sample_url"]))
            if e["status"] != "live":
                bits.append("status: %s" % e["status"])
            lines.append("- %s: %s%s (%s)" % (e["name"], base, e["path"], "; ".join(bits)))
    if PLANS:
        lines += ["", "## Passes", "- Checkout: %s/buy" % base]
        lines += ["- %s: %s (token valid %d days)" % (p.display_name(), ", ".join(p.paths()), p.timeout // 86400)
                  for p in PLANS]
    return "\n".join(lines) + "\n"


@app.get("/terms", response_class=PlainTextResponse, include_in_schema=False)
def terms():
    return TERMS_TEXT % {"rpm": ACCESS.requests_per_minute}


@app.get("/buy", response_class=HTMLResponse, include_in_schema=False)
def buy_page():
    nonce = secrets.token_urlsafe(16)
    csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'nonce-%s'; connect-src 'self'; "
           "img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'" % (nonce, nonce))
    return HTMLResponse(BUY_HTML.replace("__NONCE__", nonce),
                        headers={"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
                                 "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Operator dashboard
# ---------------------------------------------------------------------------
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AurAgrid Command Center - Mainnet</title>
    <script src="https://cdn.tailwindcss.com/3.4.17"></script>
    <link href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css" rel="stylesheet">
    <style>
        @import url("https://fonts.googleapis.com/css2?family=Inter:wght@400;600;800&family=JetBrains+Mono&display=swap");
        body { background-color: #0f172a; color: #f8fafc; font-family: "Inter", sans-serif; }
        .font-mono { font-family: "JetBrains Mono", monospace; }
        .glass-card {
            background: rgba(30, 41, 59, 0.7);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.05);
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.2);
        }
        .scrollbar-hide::-webkit-scrollbar { display: none; }
        .scrollbar-hide { -ms-overflow-style: none; scrollbar-width: none; }
        :root {
            --s-paid: #3987e5; --s-free: #d95926; --s-quote: #199e70; --s-discovery: #c98500;
            --ok: #10b981; --bad: #f87171; --warn: #fbbf24;
        }
        .chart { display: flex; align-items: flex-end; gap: 6px; height: 180px; border-bottom: 1px solid #334155; }
        .chart .col { flex: 1; height: 100%; display: flex; flex-direction: column; justify-content: flex-end;
                      gap: 2px; cursor: default; border-radius: 4px; }
        .chart .col:hover { background: rgba(148, 163, 184, 0.08); }
        .chart .seg { width: 70%; margin: 0 auto; min-height: 2px; }
        .chart .seg.top { border-radius: 4px 4px 0 0; }
        .chart-x { display: flex; gap: 6px; }
        .chart-x span { flex: 1; text-align: center; }
        .swatch { display: inline-block; width: 10px; height: 10px; border-radius: 2px; margin-right: 6px; }
        #tooltip { position: fixed; pointer-events: none; z-index: 60; display: none; }
    </style>
</head>
<body class="min-h-screen flex flex-col bg-slate-900 bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-800 via-slate-900 to-black">
    <header class="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-0 z-50 px-4 md:px-8 py-5 flex flex-wrap gap-2 justify-between items-center shadow-sm">
        <div class="flex items-center space-x-4">
            <div class="relative flex h-4 w-4">
              <span id="pulse" class="animate-ping absolute inline-flex h-full w-full rounded-full bg-slate-500 opacity-75"></span>
              <span id="dot" class="relative inline-flex rounded-full h-4 w-4 bg-slate-500"></span>
            </div>
            <h1 class="text-2xl font-black tracking-widest text-transparent bg-clip-text bg-gradient-to-r from-emerald-400 to-cyan-400">AurAgrid</h1>
            <span class="text-xs px-2.5 py-1 rounded bg-slate-800/80 text-slate-300 border border-slate-700 font-mono tracking-wide">MAINNET LND</span>
        </div>
        <div id="node-info" class="text-sm text-slate-400 font-mono flex items-center space-x-2">
            <i class="fas fa-network-wired text-slate-500"></i><span>Synchronizing...</span>
        </div>
    </header>

    <main class="flex-1 p-4 md:p-8 max-w-7xl mx-auto w-full space-y-8">
        <div id="alerts" class="rounded-xl px-5 py-3 text-sm font-semibold border bg-slate-800/60 text-slate-400 border-slate-700">
            Checking system health...
        </div>

        <!-- System health -->
        <div class="glass-card p-6 rounded-2xl">
            <div class="flex flex-wrap justify-between items-center mb-4 gap-2">
                <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 flex items-center"><i class="fas fa-heart-pulse mr-2 text-slate-500"></i>System Health</h2>
                <span id="health-checked" class="text-[11px] font-mono text-slate-500"></span>
            </div>
            <div id="svc-grid" class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 text-sm"></div>
            <div id="infra-row" class="grid grid-cols-2 lg:grid-cols-5 gap-3 mt-4 text-sm"></div>
        </div>

        <!-- Feeds -->
        <div class="glass-card p-6 rounded-2xl">
            <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 mb-4 flex items-center"><i class="fas fa-database mr-2 text-slate-500"></i>Data Feeds</h2>
            <div class="overflow-x-auto">
                <table class="w-full text-left text-sm">
                    <thead class="text-xs uppercase text-slate-500">
                        <tr><th class="pb-2 font-semibold">Feed</th><th class="pb-2 font-semibold">Status</th>
                            <th class="pb-2 font-semibold">Newest data</th><th class="pb-2 font-semibold">Last fetch OK</th><th class="pb-2 font-semibold">Fetch every</th>
                            <th class="pb-2 font-semibold">Stale after</th><th class="pb-2 font-semibold">Failures</th>
                            <th class="pb-2 font-semibold">Last error</th></tr>
                    </thead>
                    <tbody id="feeds-tbody" class="divide-y divide-slate-700/50 font-mono text-xs whitespace-nowrap [&_td]:pr-4"></tbody>
                </table>
            </div>
        </div>

        <!-- Traffic -->
        <div class="glass-card p-6 rounded-2xl space-y-6">
            <div class="flex flex-wrap justify-between items-center gap-2">
                <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 flex items-center"><i class="fas fa-route mr-2 text-slate-500"></i>Public Traffic</h2>
                <div class="flex items-center gap-1 text-xs font-mono" id="range-btns">
                    <button data-days="1" class="px-2.5 py-1 rounded border border-slate-700 text-slate-400">24h</button>
                    <button data-days="7" class="px-2.5 py-1 rounded border border-slate-700 text-slate-400">7d</button>
                    <button data-days="14" class="px-2.5 py-1 rounded border border-slate-700 text-slate-400">14d</button>
                </div>
            </div>
            <div class="grid grid-cols-2 lg:grid-cols-3 gap-4" id="traffic-kpis"></div>

            <div>
                <div class="text-xs text-slate-400 mb-1">Paywall funnel</div>
                <div id="funnel" class="flex flex-wrap items-center gap-2 font-mono text-sm"></div>
            </div>

            <div>
                <div class="flex flex-wrap justify-between items-baseline gap-2 mb-3">
                    <div class="text-xs text-slate-400">Real requests per day (scanner noise excluded)</div>
                    <div id="legend" class="flex flex-wrap gap-4 text-xs text-slate-300"></div>
                </div>
                <div id="chart" class="chart"></div>
                <div id="chart-x" class="chart-x text-[10px] font-mono text-slate-500 mt-1"></div>
                <details class="mt-3 text-xs">
                    <summary class="cursor-pointer text-slate-400">Show as table</summary>
                    <table class="mt-2 w-full text-left font-mono">
                        <thead class="text-slate-500"><tr><th>Day</th><th>Paid</th><th>Free</th><th>Quotes</th><th>Discovery</th><th>Clients</th><th>Noise</th></tr></thead>
                        <tbody id="daily-tbody" class="text-slate-300"></tbody>
                    </table>
                </details>
            </div>

            <div class="grid grid-cols-1 lg:grid-cols-2 gap-6">
                <div>
                    <div class="text-xs text-slate-400 mb-2">Top real endpoints</div>
                    <div id="top-real" class="space-y-1 font-mono text-xs"></div>
                </div>
                <div>
                    <div class="text-xs text-slate-400 mb-2">Top scanner probes</div>
                    <div id="top-noise" class="space-y-1 font-mono text-xs"></div>
                </div>
            </div>

            <div>
                <div class="text-xs text-slate-400 mb-2">Latest real requests</div>
                <div class="overflow-x-auto max-h-72 overflow-y-auto scrollbar-hide">
                    <table class="w-full text-left text-xs font-mono">
                        <thead class="text-slate-500 sticky top-0 bg-slate-800/90"><tr><th class="py-1">Time (UTC)</th><th>Type</th><th>Path</th><th>Client</th><th>User agent</th></tr></thead>
                        <tbody id="recent-tbody" class="divide-y divide-slate-700/50 text-slate-300"></tbody>
                    </table>
                </div>
            </div>
            <div id="traffic-foot" class="text-[11px] font-mono text-slate-500"></div>
        </div>

        <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 pt-2 flex items-center"><i class="fas fa-coins mr-2 text-slate-500"></i>Revenue &amp; Node</h2>
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
            <div class="glass-card p-6 rounded-2xl">
                <div class="text-slate-400 text-xs font-bold uppercase tracking-wider mb-2 flex items-center"><i class="fas fa-coins mr-2 text-slate-500"></i>Settled Revenue</div>
                <div class="text-3xl font-black text-white mt-1"><span id="stat-revenue">--</span> <span class="text-sm font-medium text-slate-500">sats</span></div>
            </div>
            <div class="glass-card p-6 rounded-2xl">
                <div class="text-slate-400 text-xs font-bold uppercase tracking-wider mb-2 flex items-center"><i class="fas fa-file-invoice mr-2 text-slate-500"></i>Invoices Issued (all time)</div>
                <div id="stat-count" class="text-3xl font-black text-white mt-1">--</div>
            </div>
            <div class="glass-card p-6 rounded-2xl">
                <div class="text-slate-400 text-xs font-bold uppercase tracking-wider mb-2 flex items-center"><i class="fas fa-chart-line mr-2 text-slate-500"></i>Settlement Rate</div>
                <div id="stat-success" class="text-3xl font-black text-emerald-400 mt-1">--</div>
            </div>
            <div class="glass-card p-6 rounded-2xl">
                <div class="text-slate-400 text-xs font-bold uppercase tracking-wider mb-2 flex items-center"><i class="fas fa-satellite-dish mr-2 text-slate-500"></i>Node API</div>
                <div id="stat-node" class="text-3xl font-black text-slate-400 mt-1">--</div>
            </div>
        </div>

        <div class="glass-card px-6 py-4 rounded-xl flex flex-wrap justify-between items-center text-xs font-mono text-slate-300 gap-4">
            <div class="flex items-center space-x-2">
                <i class="fas fa-water text-cyan-400"></i>
                <span class="text-slate-400">Total Capacity:</span>
                <span id="tele-capacity" class="text-white font-bold">--</span>
            </div>
            <div class="flex items-center space-x-2">
                <i class="fas fa-percent text-emerald-400"></i>
                <span class="text-slate-400">Routing Fee Yield (APR):</span>
                <span id="tele-apr" class="text-emerald-400 font-bold">--</span>
            </div>
            <div class="flex items-center space-x-2">
                <i class="fas fa-shield-alt text-purple-400"></i>
                <span class="text-slate-400">HTLC Success Rate:</span>
                <span id="tele-htlc" class="text-purple-400 font-bold">--</span>
            </div>
            <div class="flex items-center space-x-2">
                <i class="fas fa-bolt text-amber-400"></i>
                <span class="text-slate-400">Active Channels:</span>
                <span id="tele-channels" class="text-amber-400 font-bold">--</span>
            </div>
        </div>

        <div class="glass-card p-6 rounded-2xl">
            <h2 class="text-sm font-bold uppercase tracking-wider text-slate-400 mb-4 flex items-center"><i class="fas fa-layer-group mr-2 text-slate-500"></i>Revenue by Plan</h2>
            <div id="plan-list" class="grid grid-cols-1 md:grid-cols-4 gap-4 font-mono text-sm"></div>
        </div>

        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <div class="glass-card p-7 rounded-2xl flex flex-col h-[420px]">
                <h2 class="text-xl font-bold text-white mb-6 flex items-center"><i class="fas fa-server mr-3 text-cyan-400"></i> L402 Services Revenue</h2>
                <div id="services-list" class="space-y-4 flex-1 overflow-y-auto scrollbar-hide pr-2">
                    <div class="text-slate-500 text-sm animate-pulse font-mono">Loading breakdown...</div>
                </div>
            </div>

            <div class="glass-card p-7 rounded-2xl flex flex-col h-[420px]">
                <h2 class="text-xl font-bold text-white mb-6 flex items-center"><i class="fas fa-bolt mr-3 text-amber-400"></i> Recent Invoices</h2>
                <div class="overflow-hidden flex-1">
                    <div class="h-full overflow-y-auto scrollbar-hide pr-2">
                        <table class="w-full text-left text-sm">
                            <thead class="text-xs uppercase text-slate-500 sticky top-0 bg-slate-800/90 backdrop-blur z-10">
                                <tr>
                                    <th class="pb-3 pt-2 pl-2 font-semibold">Memo</th>
                                    <th class="pb-3 pt-2 font-semibold">Amount</th>
                                    <th class="pb-3 pt-2 pr-2 font-semibold text-right">Status</th>
                                </tr>
                            </thead>
                            <tbody id="tx-tbody" class="divide-y divide-slate-700/50"></tbody>
                        </table>
                    </div>
                </div>
            </div>
        </div>
    </main>

    <div class="fixed bottom-4 right-4 z-50 px-3.5 py-2 rounded-lg font-mono text-xs bg-slate-900 border border-slate-700 text-slate-300">
        <span id="sync-label">Waiting for first sync</span>
    </div>
    <div id="tooltip" class="px-3 py-2 rounded-lg bg-slate-950 border border-slate-600 text-xs font-mono text-slate-200 shadow-lg"></div>

    <script>
        const $ = (id) => document.getElementById(id);
        const num = (n) => (n === null || n === undefined) ? "n/a" : Number(n).toLocaleString();
        const pct = (v) => (v === null || v === undefined) ? "n/a" : String(v);

        function el(tag, cls, text) {
            const e = document.createElement(tag);
            if (cls) e.className = cls;
            if (text !== undefined) e.textContent = text;   // textContent: never parsed as HTML
            return e;
        }

        async function getJSON(url) {
            const r = await fetch(url, { credentials: "same-origin" });
            if (!r.ok) throw new Error(url + " returned " + r.status);
            return r.json();
        }

        function setStatus(ok, label) {
            const color = ok ? "bg-emerald-500" : "bg-red-500";
            $("dot").className = "relative inline-flex rounded-full h-4 w-4 " + color;
            $("pulse").className = "animate-ping absolute inline-flex h-full w-full rounded-full opacity-75 " + color;
            $("sync-label").textContent = label;
            $("stat-node").textContent = ok ? "Reachable" : "Unreachable";
            $("stat-node").className = "text-3xl font-black mt-1 " + (ok ? "text-cyan-400" : "text-red-400");
        }

        function renderServices(rows) {
            const list = $("services-list");
            list.replaceChildren();
            rows.forEach((svc) => {
                const row = el("div", "flex justify-between items-center p-4 bg-slate-800/40 rounded-xl border border-slate-700/50");
                row.appendChild(el("span", "text-sm font-semibold text-slate-200", svc.service_name));
                const right = el("div", "text-right");
                right.appendChild(el("div", "text-sm font-black text-emerald-400", num(svc.total_revenue_sats) + " sats"));
                right.appendChild(el("div", "text-[10px] font-mono text-slate-500", num(svc.calls) + " calls"));
                row.appendChild(right);
                list.appendChild(row);
            });
        }

        const PLAN_LABELS = { hourly: "Hourly", pass_30d: "30-day Pass", bundle: "Bundle", unattributed: "Unattributed" };

        function renderPlans(revenue, usage) {
            const list = $("plan-list");
            list.replaceChildren();
            Object.keys(revenue).forEach((kind) => {
                const box = el("div", "p-4 bg-slate-800/40 rounded-xl border border-slate-700/50");
                box.appendChild(el("div", "text-xs text-slate-400 uppercase", PLAN_LABELS[kind] || kind));
                box.appendChild(el("div", "text-xl font-black text-emerald-400", num(revenue[kind].revenue_sats) + " sats"));
                const u = usage[kind] || { tokens: 0, calls: 0 };
                box.appendChild(el("div", "text-[10px] text-slate-500",
                    num(revenue[kind].settled_invoices) + " invoices, " + num(u.tokens) + " tokens, " + num(u.calls) + " calls"));
                list.appendChild(box);
            });
        }

        function renderTransactions(txs) {
            const body = $("tx-tbody");
            body.replaceChildren();
            if (txs.length === 0) {
                const tr = el("tr");
                const td = el("td", "py-6 text-slate-500 text-center font-mono text-xs", "No invoices recorded yet.");
                td.colSpan = 3;
                tr.appendChild(td);
                body.appendChild(tr);
                return;
            }
            txs.forEach((tx) => {
                const tr = el("tr");
                const memo = el("td", "py-3.5 pl-2 font-mono text-xs text-slate-300 max-w-[180px] truncate", tx.memo);
                memo.setAttribute("title", tx.memo);
                const amt = el("td", "py-3.5 font-bold text-white", num(tx.amount_sat) + " sats");
                const state = el("td", "py-3.5 pr-2 text-right");
                const settled = tx.state === "settled";
                state.appendChild(el(
                    "span",
                    "px-2.5 py-1 rounded-md text-[10px] font-bold uppercase tracking-wide border " +
                        (settled
                            ? "bg-emerald-500/10 text-emerald-400 border-emerald-500/20"
                            : "bg-amber-500/10 text-amber-400 border-amber-500/20"),
                    settled ? "Settled" : "Unpaid"
                ));
                tr.append(memo, amt, state);
                body.appendChild(tr);
            });
        }

        async function refresh() {
            if (document.hidden) return;
            try {
                const [stats, info, txs] = await Promise.all([
                    getJSON("/api/proxy/stats"),
                    getJSON("/api/proxy/info"),
                    getJSON("/api/proxy/transactions?limit=100"),
                ]);

                $("stat-revenue").textContent = num(stats.total_revenue_sats);
                $("stat-count").textContent = num(stats.transaction_count);
                $("stat-success").textContent = stats.success_rate + "%";
                $("tele-capacity").textContent = stats.total_capacity_sats == null ? "n/a" : num(stats.total_capacity_sats) + " sats";
                $("tele-apr").textContent = pct(stats.forwarding_fee_apr);
                $("tele-htlc").textContent = pct(stats.htlc_success_rate);
                $("tele-channels").textContent = num(stats.active_channels);

                const nodeInfo = $("node-info");
                nodeInfo.replaceChildren(
                    el("span", "text-cyan-400 font-bold", info.node_alias || "unknown node"),
                    el("span", "text-slate-500", "(" + (info.network || "n/a") + ")"),
                    el("span", "text-slate-600 px-2", "|"),
                    el("span", "text-slate-300", "Channels: " + num(info.active_channels))
                );

                renderServices(stats.service_breakdown);
                renderPlans(stats.revenue_by_plan || {}, stats.usage_by_plan || {});
                renderTransactions(txs.transactions);
                setStatus(true, "Synced " + new Date().toLocaleTimeString());
            } catch (err) {
                console.error("Dashboard refresh failed:", err);
                setStatus(false, "Sync failed, retrying");
            }
        }

        // ---------------- Health & traffic ----------------
        const SERIES = [
            { key: "paid", label: "Paid", color: "var(--s-paid)" },
            { key: "free", label: "Free", color: "var(--s-free)" },
            { key: "quote", label: "Quotes", color: "var(--s-quote)" },
            { key: "discovery", label: "Discovery", color: "var(--s-discovery)" },
        ];
        let trafficDays = 7;
        try { trafficDays = Number(localStorage.getItem("ag-days")) || 7; } catch (e) {}

        function ago(sec) {
            if (sec === null || sec === undefined) return "never";
            sec = Math.max(0, Math.round(sec));
            if (sec < 90) return sec + "s ago";
            if (sec < 5400) return Math.round(sec / 60) + "m ago";
            if (sec < 172800) return (sec / 3600).toFixed(1) + "h ago";
            return (sec / 86400).toFixed(1) + "d ago";
        }
        function dur(sec) {
            if (sec >= 86400) return (sec / 86400).toFixed(sec % 86400 ? 1 : 0) + "d";
            if (sec >= 3600) return (sec / 3600).toFixed(sec % 3600 ? 1 : 0) + "h";
            return Math.round(sec / 60) + "m";
        }
        function badge(ok, okText, badText) {
            return el("span", "inline-flex items-center gap-1.5 font-semibold " + (ok ? "text-emerald-400" : "text-red-400"),
                (ok ? "✓ " : "✗ ") + (ok ? okText : badText));
        }
        function tile(label, value, sub, valueCls) {
            const box = el("div", "p-4 bg-slate-800/40 rounded-xl border border-slate-700/50");
            box.appendChild(el("div", "text-xs text-slate-400", label));
            box.appendChild(el("div", "text-2xl font-black mt-1 " + (valueCls || "text-white"), value));
            if (sub) box.appendChild(el("div", "text-[11px] text-slate-500 mt-0.5", sub));
            return box;
        }

        function renderHealth(h) {
            const alerts = $("alerts");
            const notices = h.notices || [];
            const list = (items) => {
                const ul = el("ul", "list-disc ml-6 mt-1 font-normal");
                items.forEach((a) => ul.appendChild(el("li", "", a)));
                return ul;
            };
            if (!h.ok) {
                alerts.className = "rounded-xl px-5 py-3 text-sm font-semibold border bg-red-500/10 text-red-300 border-red-500/30";
                const n = h.alerts.length;
                alerts.replaceChildren(el("div", "", "⚠ " + n + (n > 1 ? " issues need" : " issue needs") + " attention"), list(h.alerts));
                if (notices.length) alerts.append(el("div", "mt-2 text-amber-300", "Notices"), list(notices));
            } else if (notices.length) {
                alerts.className = "rounded-xl px-5 py-3 text-sm font-semibold border bg-amber-500/10 text-amber-200 border-amber-500/30";
                alerts.replaceChildren(el("div", "", "✓ All systems running · " + notices.length + " notice" + (notices.length > 1 ? "s" : "")), list(notices));
            } else {
                alerts.className = "rounded-xl px-5 py-3 text-sm font-semibold border bg-emerald-500/10 text-emerald-300 border-emerald-500/30";
                alerts.textContent = "✓ All systems normal";
            }
            $("health-checked").textContent = "checked " + new Date(h.checked_at * 1000).toLocaleTimeString();

            const now = h.checked_at;
            const grid = $("svc-grid");
            grid.replaceChildren();
            h.services.forEach((s) => {
                const box = el("div", "p-3 bg-slate-800/40 rounded-xl border " + (s.ok ? "border-slate-700/50" : "border-red-500/50"));
                const top = el("div", "flex justify-between items-center gap-2");
                top.appendChild(el("span", "text-slate-200 font-semibold truncate", s.label));
                top.appendChild(badge(s.ok, s.state, s.state));
                box.appendChild(top);
                let sub = s.name + (s.kind === "process" ? " (process)" : "");
                if (s.since) sub += " · up " + ago(now - s.since).replace(" ago", "");
                if (s.restarts) sub += " · " + s.restarts + " restarts since boot";
                if (s.next_run) sub = "last " + ago(now - s.last_run) + " (" + (s.last_result || "?") + ") · next in " + ago(s.next_run - now).replace(" ago", "");
                box.appendChild(el("div", "text-[11px] font-mono text-slate-500 mt-1 truncate", sub));
                grid.appendChild(box);
            });

            const c = h.cert, d = h.disk;
            $("infra-row").replaceChildren(
                tile("TLS certificate", c.error ? "error" : c.days_left + " days",
                     c.error ? c.error : "expires " + new Date(c.not_after * 1000).toUTCString().slice(5, 22) + " UTC",
                     c.ok ? "text-white" : "text-red-400"),
                tile("Response signing", h.signing_enabled ? "On" : "Off", "X-AurAgrid-Signature-V2",
                     h.signing_enabled ? "text-white" : "text-red-400"),
                tile("Disk used", d.used_pct + "%", d.free_gb + " GB free", d.ok ? "text-white" : "text-red-400"),
                tile("Feed database", h.feeds_db_mb + " MB", "var/feeds.db"),
                (() => {
                    const sc = h.selfcheck || {};
                    if (!sc.last_run) return tile("Weekly self-check", "none yet", sc.error || "", "text-slate-400");
                    return tile("Weekly self-check", sc.PASS + " pass · " + sc.FAIL + " fail",
                                "last run " + ago(h.checked_at - sc.last_run) + (sc.WARN ? " · " + sc.WARN + " warnings" : ""),
                                sc.ok ? "text-white" : "text-red-400");
                })(),
            );

            const body = $("feeds-tbody");
            body.replaceChildren();
            h.feeds.forEach((f) => {
                const tr = el("tr");
                const st = el("td", "py-2");
                st.appendChild(f.enabled ? badge(f.ok, "fresh", "stale") : el("span", "text-slate-500", "disabled"));
                const err = el("td", "py-2 text-red-300 max-w-[260px] truncate", f.last_error || "");
                if (f.last_error) err.title = f.last_error;
                tr.append(el("td", "py-2 text-slate-200", f.feed), st, el("td", "py-2", ago(f.age_s)), el("td", "py-2 text-slate-400", ago(f.last_success_age_s)),
                          el("td", "py-2 text-slate-400", dur(f.interval_s)), el("td", "py-2 text-slate-400", dur(f.stale_after_s)),
                          el("td", "py-2 " + (f.failures ? "text-red-300" : "text-slate-400"), String(f.failures)), err);
                body.appendChild(tr);
            });
        }

        function showTip(evt, lines) {
            const tip = $("tooltip");
            tip.replaceChildren(...lines.map((l) => el("div", l.cls || "", l.text)));
            tip.style.display = "block";
            const x = Math.min(evt.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
            tip.style.left = x + "px";
            tip.style.top = (evt.clientY + 14) + "px";
        }
        function hideTip() { $("tooltip").style.display = "none"; }

        function renderChart(daily) {
            const chart = $("chart"), xs = $("chart-x");
            chart.replaceChildren(); xs.replaceChildren();
            const legend = $("legend");
            legend.replaceChildren(...SERIES.map((s) => {
                const item = el("span", "flex items-center");
                const sw = el("span", "swatch"); sw.style.background = s.color;
                item.append(sw, document.createTextNode(s.label));
                return item;
            }));
            const totals = daily.map((d) => SERIES.reduce((a, s) => a + d[s.key], 0));
            const max = Math.max(...totals);
            if (max === 0) {
                chart.appendChild(el("div", "w-full self-center text-center text-slate-500 text-sm", "No real requests in this window. Only scanners."));
            }
            const step = daily.length > 7 ? 2 : 1;
            daily.forEach((d, i) => {
                const col = el("div", "col");
                if (max > 0) {
                    const segs = SERIES.filter((s) => d[s.key] > 0).reverse();  // top of stack first
                    segs.forEach((s, j) => {
                        const seg = el("div", "seg" + (j === 0 ? " top" : ""));
                        seg.style.background = s.color;
                        seg.style.height = (d[s.key] / max * 100) + "%";
                        col.appendChild(seg);
                    });
                }
                const lines = [{ text: d.day, cls: "text-slate-400 mb-1" }]
                    .concat(SERIES.map((s) => ({ text: s.label + ": " + num(d[s.key]) })))
                    .concat([{ text: "Clients: " + num(d.clients) }, { text: "Scanner noise: " + num(d.noise), cls: "text-slate-500" }]);
                col.addEventListener("mousemove", (e) => showTip(e, lines));
                col.addEventListener("mouseleave", hideTip);
                chart.appendChild(col);
                xs.appendChild(el("span", "", i % step === 0 ? d.day.slice(5) : ""));
            });

            const tb = $("daily-tbody");
            tb.replaceChildren();
            daily.slice().reverse().forEach((d) => {
                const tr = el("tr");
                [d.day, d.paid, d.free, d.quote, d.discovery, d.clients, d.noise].forEach((v) => tr.appendChild(el("td", "pr-3", String(v))));
                tb.appendChild(tr);
            });
        }

        function renderTop(target, rows, empty) {
            const box = $(target);
            box.replaceChildren();
            if (!rows.length) { box.appendChild(el("div", "text-slate-500", empty)); return; }
            const max = rows[0].requests;
            rows.forEach((r) => {
                const row = el("div", "flex items-center gap-3");
                const bar = el("div", "h-1.5 rounded bg-slate-500/60");
                bar.style.width = Math.max(4, r.requests / max * 80) + "px";
                const label = el("span", "flex-1 truncate text-slate-300", r.path); label.title = r.path;
                row.append(label, bar, el("span", "w-12 text-right text-slate-400", num(r.requests)));
                box.appendChild(row);
            });
        }

        function renderTraffic(t) {
            document.querySelectorAll("#range-btns button").forEach((b) => {
                const on = Number(b.dataset.days) === t.days;
                b.className = "px-2.5 py-1 rounded border " + (on ? "border-cyan-500 text-cyan-300 bg-cyan-500/10" : "border-slate-700 text-slate-400");
            });
            const T = t.totals, real = T.paid + T.free + T.quote;
            $("traffic-kpis").replaceChildren(
                tile("API calls", num(real), num(T.paid) + " paid · " + num(T.free) + " free · " + num(T.quote) + " quotes"),
                tile("Discovery hits", num(T.discovery), "manifest, llms.txt, openapi, /buy"),
                tile("Real clients", num(t.clients.real), "distinct IPs, excluding scanners"),
                tile("Scanner noise", num(T.noise), num(t.clients.noise) + " IPs probing other paths", "text-slate-400"),
                tile("Free sample hits", num(T.sample_hits), "delayed/daily copies of the paid feeds"),
                tile("Stale refusals", num(T.stale_refusals), "paid requests answered 503, not charged",
                     T.stale_refusals ? "text-amber-300" : "text-white"),
            );

            const f = t.funnel, fun = $("funnel");
            fun.replaceChildren();
            if (!f) {
                fun.appendChild(el("span", "text-red-300", "LND unavailable, invoice data missing"));
            } else {
                const steps = [["requests to paid routes", f.paid_requests], ["invoices issued", f.invoices_issued],
                               ["paid", f.invoices_settled], ["sats earned", f.sats_settled]];
                steps.forEach(([label, v], i) => {
                    if (i) fun.appendChild(el("i", "fas fa-arrow-right text-slate-600 mx-1"));
                    const s = el("span", "px-3 py-1.5 rounded-lg bg-slate-800/60 border border-slate-700/50");
                    s.append(el("b", "text-white", num(v) + " "), el("span", "text-slate-400", label));
                    fun.appendChild(s);
                });
            }

            renderChart(t.daily);
            const realTop = ["paid", "free", "quote", "discovery"]
                .flatMap((c) => t.top_paths[c]).sort((a, b) => b.requests - a.requests).slice(0, 8);
            renderTop("top-real", realTop, "No real requests yet.");
            renderTop("top-noise", t.top_paths.noise, "None.");

            const body = $("recent-tbody");
            body.replaceChildren();
            if (!t.recent.length) {
                const tr = el("tr"); const td = el("td", "py-3 text-slate-500", "No real requests yet."); td.colSpan = 5;
                tr.appendChild(td); body.appendChild(tr);
            }
            t.recent.forEach((r) => {
                const tr = el("tr");
                const sw = el("span", "swatch");
                sw.style.background = (SERIES.find((s) => s.key === r.category) || {}).color || "#64748b";
                const type = el("td", "py-1.5 pr-3 whitespace-nowrap"); type.append(sw, document.createTextNode(r.category));
                const ua = el("td", "py-1.5 text-slate-500 max-w-[280px] truncate", r.user_agent); ua.title = r.user_agent;
                tr.append(el("td", "py-1.5 pr-3 whitespace-nowrap", r.time), type,
                          el("td", "py-1.5 pr-3 text-slate-200", r.method + " " + r.path), el("td", "py-1.5 pr-3", r.ip), ua);
                body.appendChild(tr);
            });

            const starts = t.aperture_starts.length ? t.aperture_starts[t.aperture_starts.length - 1] + " UTC" : "n/a";
            $("traffic-foot").textContent = "Source: " + t.log_path + " · last Aperture start " + starts +
                " · " + num(t.self_requests) + " requests from this server excluded";
        }

        async function refreshOps() {
            if (document.hidden) return;
            const [h, t] = await Promise.allSettled([getJSON("/api/proxy/health"), getJSON("/api/proxy/traffic?days=" + trafficDays)]);
            if (h.status === "fulfilled") renderHealth(h.value);
            else { $("alerts").className = "rounded-xl px-5 py-3 text-sm font-semibold border bg-red-500/10 text-red-300 border-red-500/30";
                   $("alerts").textContent = "⚠ Health check failed: " + h.reason.message; }
            if (t.status === "fulfilled") renderTraffic(t.value);
        }

        document.querySelectorAll("#range-btns button").forEach((b) => b.addEventListener("click", () => {
            trafficDays = Number(b.dataset.days);
            try { localStorage.setItem("ag-days", String(trafficDays)); } catch (e) {}
            refreshOps();
        }));
        document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); refreshOps(); } });

        refreshOps();
        setInterval(refreshOps, 30000);
        refresh();
        setInterval(refresh, 8000);
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)], include_in_schema=False)
def serve_dashboard():
    return DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Derived endpoints (derived.py): free previews, computed from stored feeds
# ---------------------------------------------------------------------------
class DerivedResponse(BaseModel):
    feed: str
    method: str = "derived"
    plan: str = "free"
    served_at: str
    stale: bool = Field(description="True when any input is older than its stale limit")
    provenance: dict = Field(description="source, derivation (the formula), inputs with as_of and stale flags")
    data: dict


_derived_cache: dict = {}
DERIVED_TTL_SECONDS = 60.0


def _derived_response(key: str, *args):
    """Compute (or reuse for 60 s) a derived result; 503 with the stale input when one is unusable."""
    cache_key = (key,) + args
    hit = _derived_cache.get(cache_key)
    if hit and time.monotonic() - hit[0] < DERIVED_TTL_SECONDS:
        result = hit[1]
    else:
        try:
            result = derived.ENDPOINTS[key]["func"](STORE, *args)
        except derived.InputUnavailable as exc:
            return JSONResponse({"detail": "An input feed has no usable data; not served.", "feed": key,
                                 "stale": True, "input": exc.feed, "as_of": exc.as_of, "status_url": "/status.json"},
                                status_code=503, headers={"Retry-After": "300", "Cache-Control": "no-store"})
        if len(_derived_cache) > 256:
            _derived_cache.clear()
        _derived_cache[cache_key] = (time.monotonic(), result)
    return {"feed": result["feed"], "method": "derived", "plan": "free",
            "served_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stale": result["stale"], "provenance": result["provenance"], "data": result["data"]}


_DERIVED_RESPONSES = {200: {"model": DerivedResponse},
                      503: {"description": "An input feed has no usable data (stale=true, as_of of that input)"}}


@app.get("/api/v1/derived/fee-timing", tags=["free", "derived"], responses=_DERIVED_RESPONSES,
         summary=derived.ENDPOINTS["derived/fee-timing"]["name"],
         description=derived.ENDPOINTS["derived/fee-timing"]["description"])
def derived_fee_timing():
    return _derived_response("derived/fee-timing")


@app.get("/api/v1/derived/area-risk", tags=["free", "derived"], responses=_DERIVED_RESPONSES,
         summary=derived.ENDPOINTS["derived/area-risk"]["name"],
         description=derived.ENDPOINTS["derived/area-risk"]["description"])
def derived_area_risk(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
                      radius_km: float = Query(500, ge=10, le=2000)):
    return _derived_response("derived/area-risk", round(lat, 3), round(lon, 3), round(radius_km))


@app.get("/api/v1/derived/energy-snapshot", tags=["free", "derived"], responses=_DERIVED_RESPONSES,
         summary=derived.ENDPOINTS["derived/energy-snapshot"]["name"],
         description=derived.ENDPOINTS["derived/energy-snapshot"]["description"])
def derived_energy_snapshot():
    return _derived_response("derived/energy-snapshot")


@app.get("/api/v1/derived/grid-carbon", tags=["free", "derived"], responses=_DERIVED_RESPONSES,
         summary=derived.ENDPOINTS["derived/grid-carbon"]["name"],
         description=derived.ENDPOINTS["derived/grid-carbon"]["description"])
def derived_grid_carbon():
    return _derived_response("derived/grid-carbon")


# ---------------------------------------------------------------------------
# OpenAPI: generated from the live routes, plus L402 details with live prices
# ---------------------------------------------------------------------------
L402_FLOW = ("L402 payment required. The WWW-Authenticate header carries a macaroon and a BOLT11 invoice "
             "(`L402 macaroon=\"...\", invoice=\"lnbc...\"`). Pay the invoice with a Lightning wallet, then "
             "repeat the request with `Authorization: L402 <macaroon>:<preimage-hex>`. The token is valid for "
             "x-l402.token_ttl_seconds; reuse it instead of paying again. Check x-l402.quote_url first: an "
             "invoice should be within the quote's tolerance_pct. A free sample is at x-l402.sample_url.")


def _l402_ext(path: str) -> dict:
    q = engine.quote(path) or {}
    ext = {"price_sats": q.get("price_sats"), "price_usd_approx": _usd(q.get("price_sats")),
           "pricing": q.get("pricing"), "token_ttl_seconds": q.get("token_ttl_seconds"),
           "quote_url": "/api/v1/price/" + path[len("/api/v1/"):]}
    if q.get("usd_price") is not None:
        ext["usd_price"] = q["usd_price"]
    sample = sample_url(engine.feed_key(path))
    if sample:
        ext["sample_url"] = sample
    return ext


def custom_openapi():
    spec = get_openapi(title="AurAgrid data oracle", version="1.0.0", routes=app.routes,
                       description=("Bitcoin fee and Lightning network data from AurAgrid's own nodes (paid, L402) "
                                    "and U.S. public-domain feeds (EIA, NOAA, USGS; free). Every data response "
                                    "carries provenance and an LND-signed X-AurAgrid-Signature-V2. Feed freshness: "
                                    "/status.json. Machine manifest: /agent-manifest.json."))
    if PUBLIC_URL:
        spec["servers"] = [{"url": PUBLIC_URL}]
    spec.setdefault("components", {}).setdefault("securitySchemes", {})["L402"] = {
        "type": "apiKey", "in": "header", "name": "Authorization",
        "description": "`L402 <macaroon>:<preimage>` obtained by paying the 402 challenge's invoice"}
    paths = spec.setdefault("paths", {})
    feed_get = {}
    for slug, *_ in PAID_CATALOG:
        path = "/api/v1/" + slug
        op = paths.get(path, {}).get("get")
        if not op:
            continue
        feed_get[feed_key(slug)] = op
        op["security"] = [{"L402": []}]
        op["responses"]["402"] = {"description": L402_FLOW,
                                  "headers": {"WWW-Authenticate": {"schema": {"type": "string"},
                                                                   "description": "L402 challenge"}}}
        op["x-l402"] = _l402_ext(path)
    for plan in PLANS:   # plan paths are served by middleware, so they are not in app.routes
        for feed, path in zip(plan.feeds, plan.paths()):
            base_op = feed_get.get(feed)
            if base_op is None:
                continue
            op = json.loads(json.dumps(base_op))
            op["operationId"] = "%s_%s" % (plan.name.replace("-", "_"), feed.replace("-", "_").replace("/", "_"))
            op["summary"] = "%s (%s)" % (plan.display_name(), feed)
            op["tags"] = ["paid", "plans"]
            op["x-l402"] = _l402_ext(path)
            paths[path] = {"get": op}
    return spec


app.openapi = custom_openapi
