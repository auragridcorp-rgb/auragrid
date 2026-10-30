"""AurAgrid backend: paid feeds, passes/bundles, free samples, operator dashboard.

Run behind Aperture only, bound to loopback:

    uvicorn app.main:app --host 127.0.0.1 --port 8000

Routes
------
/api/v1/<feed>                 paid hourly feeds (Aperture sells the token)
/api/v1/<plan prefix>/<feed>   30-day passes and bundles, rewritten onto the
                               feed routes by pass_support.PlanRewriteMiddleware
/api/v1/sample/<feed>          free, delayed, truncated samples (samples.py)
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
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import feeds
from app.buy_page import BUY_HTML
from app.lnd_client import lnd
from attestation import AttestationMiddleware, lnd_rest_signer
from l402_ledger import L402LedgerMiddleware, Ledger, hash_hex, token_payment_hash
from pass_support import HOURLY, PlanRewriteMiddleware, PlanRouter, load_access, load_plans
from pricer import PriceEngine, normalize_path
from samples import SAMPLE_FEEDS, SampleStore, resolve as resolve_sample
from sample_examples import example_body
import eia_grid_feed
import eia_lng_feed
import eia_spr_feed
import ercot_feed
import ndbc_feed
import portwatch_feed

ercot_feed.register_from_env()       # ercot-lmp, when ERCOT credentials are configured
portwatch_feed.register_from_env()   # port-congestion-index, when AURAGRID_PORTWATCH_ENABLED=1
eia_lng_feed.register_from_env()     # lng-supply-chain, when EIA_API_KEY is configured
eia_grid_feed.register_from_env()    # energy/grid, same key
eia_spr_feed.register_from_env()     # spr-stockpile, same key
ndbc_feed.register_from_env()        # maritime-weather (NOAA buoys; no key needed)

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
    sample_store.start()
    yield
    sample_store.stop()
    engine.stop()


app = FastAPI(title="AurAgrid Command Center", lifespan=lifespan)
security = HTTPBasic()


# ---------------------------------------------------------------------------
# Catalog: single source of truth for endpoints, names, and (optional) prices
# ---------------------------------------------------------------------------
CATALOG = [
    # slug (path after /api/v1/), display name, tier, description
    ("esg/carbon", "ESG Carbon", "retail",
     "Satellite-verified ESG carbon offset telemetry."),
    ("energy/grid", "U.S. Grid Demand and Generation Mix", "retail",
     "Hourly demand, forecast, interchange and generation by fuel with renewable/carbon-free shares "
     "for the Lower 48, EIA regions and every balancing authority (U.S. EIA-930)."),
    ("sar/commodities", "SAR Commodities", "retail",
     "Synthetic aperture radar (SAR) commodity volume estimates."),
    ("maritime/congestion", "Maritime Congestion", "retail",
     "Global shipping chokepoint vessel queues and delays."),
    ("weather/micro-climate", "Weather Micro Climate", "retail",
     "High-frequency pressure and wind vector telemetry."),
    ("logistics/cross-border", "Logistics Cross Border", "retail",
     "Manufacturing yard density and freight velocity."),
    ("batch/hyperscaler-power", "Batch Hyperscaler Power Forecast", "wholesale",
     "Power load predictions for AI data centers."),
    ("batch/lng-supply-chain", "U.S. LNG Export Monitor", "wholesale",
     "Monthly U.S. LNG exports by terminal and destination country, export prices, and month/year "
     "changes (U.S. EIA; about three months' publication lag)."),
    ("batch/crop-moisture", "Batch Crop Moisture Matrix", "wholesale",
     "Soil moisture and precipitation matrices across agricultural belts."),
    ("batch/sts-anomalies", "Batch STS Transfer Anomalies", "wholesale",
     "Open-ocean ship-to-ship (STS) cargo transfer tracking."),
    ("batch/maritime-weather", "Marine Weather Observations", "wholesale",
     "Latest wind, gust, wave height/period/direction, pressure and air/sea temperature from "
     "~850 NOAA NDBC buoys and coastal stations worldwide (updated continuously)."),
    ("batch/curtailment-risk", "Batch Renewable Curtailment Risk", "wholesale",
     "Renewable energy curtailment predictions for wind/solar."),
    ("batch/dark-fleet-matrix", "Global Dark Fleet Gap Matrix", "wholesale",
     "Dark fleet AIS anomaly and vessel spoofing matrix."),
    ("batch/ercot-lmp", "ERCOT 4,000-Node LMP Aggregation", "wholesale",
     "ERCOT 4000-node locational marginal pricing grid."),
    ("batch/port-congestion-index", "Top 100 Global Port Activity Index", "wholesale",
     "Port calls and import/export tonnage for the 100 busiest ports vs. their 90-day baseline, "
     "plus chokepoint transits (IMF PortWatch, satellite AIS; updated weekly)."),
    ("batch/carbon-credit-audit", "Multi-Project Carbon Credit Audit Matrix", "wholesale",
     "Satellite carbon credit verification matrix."),
    ("batch/spr-stockpile", "U.S. Strategic Petroleum Reserve Monitor", "wholesale",
     "Weekly SPR and commercial crude stocks with 1/4/52-week changes, 52-week range and history "
     "(U.S. EIA Weekly Petroleum Status Report)."),
    ("batch/critical-minerals", "Batch Critical Minerals Tracker", "wholesale",
     "Critical minerals and rare-earth supply chain tracker."),
    ("batch/naval-escalation", "Batch Naval Escalation Risk", "wholesale",
     "Geopolitical naval escalation risk matrix."),
    ("batch/wildfire-carbon-offset", "Batch Wildfire Carbon Offset Risk", "wholesale",
     "Wildfire risk matrix for carbon offset forests."),
    ("batch/green-compute", "Batch Green Compute Routing", "wholesale",
     "Zero-carbon data center compute routing matrix."),
    ("batch/agri-yield", "Batch Agri-Commodities Yield Predictor", "wholesale",
     "Global agricultural crop yield predictor."),
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


def feed_key(slug: str) -> str:
    return normalize_path("/api/v1/" + slug)


# Only feeds listed in pricing.yaml `access:` are offered; the rest have no data source yet.
OFFERED = [entry for entry in CATALOG if feed_key(entry[0]) in ACCESS.offered]
PAID_CATALOG = [entry for entry in OFFERED if feed_key(entry[0]) in ACCESS.paid]
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
    paths += ["/api/v1/sample/" + key for key in SAMPLE_FEEDS] + ["/buy"]
    dead = [svc.get("name") for svc, rx in rules if not any(rx.match(p) for p in paths)]
    return pricing, unmatched, dead


engine = PriceEngine(APERTURE_CONFIG, PRICING_CONFIG)
ledger = Ledger(os.environ.get("AURAGRID_LEDGER_DB", "ledger.db"))
sample_store = SampleStore()

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
EXTRA_SERVICES = ["Mainnet Lightning Routing Liquidity"]
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
_cache = {"t": 0.0, "invoices": [], "node": {}}
_lock = threading.Lock()


def snapshot():
    with _lock:
        if time.monotonic() - _cache["t"] > CACHE_TTL_SECONDS:
            try:
                _cache["invoices"] = lnd.get_invoices()
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
        "transaction_count": total_count,
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
# Agent manifest (public)
# ---------------------------------------------------------------------------
def _catalog_entries() -> list[dict]:
    entries = []
    for slug, name, tier, desc in OFFERED:
        free = feed_key(slug) in ACCESS.free
        entry = {"path": "/api/v1/" + slug, "name": name, "tier": tier, "description": desc,
                 "access": "free" if free else "paid", "auth": "none" if free else "L402",
                 "status": "live" if feeds.get(feed_key(slug)) else "coming_soon"}
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
            if "usd_price" in quote:
                entry["usd_price"] = quote["usd_price"]
            entry["quote_url"] = "/api/v1/price/" + slug
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
            entry["quote_url"] = "/api/v1/price/" + plan.paths()[0][len("/api/v1/"):]
            if "usd_price" in quote:
                entry["usd_price"] = quote["usd_price"]
        entries.append(entry)
    return entries


def _sample_entries() -> list[dict]:
    return [{"feed": key, "path": "/api/v1/sample/" + key, "auth": "none",
             "delay_seconds": sample_store.delay, "max_items_per_list": sample_store.max_items}
            for key in SAMPLE_FEEDS]


@app.get("/.well-known/ai-plugin.json")
@app.get("/agent-manifest.json")
async def get_agent_manifest(request: Request):
    base = PUBLIC_URL or str(request.base_url).rstrip("/")
    signature = {"header": "X-AurAgrid-Signature"}
    if SIGNING_PUBKEY:
        signature["pubkey"] = SIGNING_PUBKEY
    return {
        "schema_version": "v1",
        "name_for_human": "AurAgrid L402 Oracle",
        "name_for_model": "auragrid_oracle",
        "description_for_model": (
            "AurAgrid serves %d energy and maritime data endpoints built from public sources "
            "(U.S. EIA, NOAA, ERCOT, IMF PortWatch); each response names its source. Endpoints "
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
            "token_ttl_seconds. Free, delayed and truncated samples are listed under samples. "
            "Responses carry an X-AurAgrid-Signature header; verify it against the published "
            "signing pubkey."
            % len(OFFERED)
        ),
        "api": {"type": "openapi", "url": base + "/openapi.json"},
        "terms_url": base + "/terms",
        "endpoints_count": len(OFFERED),
        "endpoints": _catalog_entries(),
        "plans": _plan_entries(),
        "samples": _sample_entries(),
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
    return quote


# ---------------------------------------------------------------------------
# Free samples (no paywall, no attestation, not in the ledger)
# ---------------------------------------------------------------------------
@app.get("/api/v1/sample/{slug}")
def get_sample(slug: str):
    """Delayed, truncated sample of a hero feed. 503 while the first snapshot ages."""
    key = resolve_sample(slug)
    if key is None:
        raise HTTPException(status_code=404, detail="No sample for this feed")
    body = sample_store.get(key)
    if body is None and not feeds.get(key) and example_body(key):
        # Not connected yet: show the response structure, clearly labeled as synthetic.
        return JSONResponse(example_body(key), headers={"Cache-Control": "public, max-age=300"})
    if body is None:
        detail = "Sample warming up" if feeds.get(key) else "Feed not connected"
        return JSONResponse({"detail": detail, "feed": key,
                             "delay_seconds": sample_store.delay},
                            status_code=503, headers={"Retry-After": str(sample_store.interval)})
    full = "/api/v1/%s/%s" % (BATCH_PREFIX, key)
    body["full_feed"] = {
        "path": full,
        "quote_url": "/api/v1/price/%s/%s" % (BATCH_PREFIX, key),
        "plans": [plan.name for plan in PLANS if key in plan.feeds],
        "checkout_url": "/buy",
    }
    return JSONResponse(body, headers={"Cache-Control": "public, max-age=60"})


# ---------------------------------------------------------------------------
# Paid feeds. Aperture sells and checks the token; passes and bundles arrive
# here rewritten by PlanRewriteMiddleware. Data comes from feeds.py providers.
# ---------------------------------------------------------------------------
def _feed_handler(slug: str):
    key = normalize_path("/api/v1/" + slug)

    def handler(request: Request):
        try:
            data = feeds.fetch(key)
        except feeds.FeedNotConnected:
            log.error("Paid request for %s, but no provider is registered in feeds.py", key)
            raise HTTPException(status_code=503, detail="Feed not connected")
        except feeds.FeedWarmingUp:
            raise HTTPException(status_code=503, detail="Feed warming up", headers={"Retry-After": "300"})
        except Exception:
            log.exception("Provider for %s failed", key)
            raise HTTPException(status_code=502, detail="Feed temporarily unavailable")
        return {
            "feed": key,
            "plan": getattr(request.state, "auragrid_plan_kind", HOURLY),
            "served_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "data": data,
        }

    return handler


for _slug, _name, _tier, _desc in OFFERED:
    _free = feed_key(_slug) in ACCESS.free
    app.add_api_route("/api/v1/" + _slug, _feed_handler(_slug), methods=["GET"], name=_slug, summary=_name,
                      description=_desc + (" Free, rate-limited per IP." if _free else " Requires L402."),
                      tags=["free" if _free else "paid"],
                      dependencies=[] if _free else [Depends(require_gateway)])
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
             "> Energy and maritime data from public sources (U.S. EIA, NOAA, ERCOT, IMF PortWatch). "
             "Free endpoints need no key; premium endpoints are paid per use over L402 (Lightning).", "",
             "- Manifest: %s/agent-manifest.json" % base,
             "- OpenAPI: %s/openapi.json" % base,
             "- Terms: %s/terms" % base]
    for title, keys in (("Free endpoints (rate-limited per IP)", ACCESS.free), ("Paid endpoints (L402)", ACCESS.paid)):
        rows = [(slug, name) for slug, name, _, _ in OFFERED if feed_key(slug) in keys]
        if rows:
            lines += ["", "## " + title]
            lines += ["- %s: %s/api/v1/%s%s" % (name, base, slug, "" if feeds.get(feed_key(slug)) else " (coming soon)")
                      for slug, name in rows]
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
    </style>
</head>
<body class="min-h-screen flex flex-col bg-slate-900 bg-[radial-gradient(ellipse_at_top,_var(--tw-gradient-stops))] from-slate-800 via-slate-900 to-black">
    <header class="border-b border-slate-800/80 bg-slate-900/60 backdrop-blur-md sticky top-0 z-50 px-8 py-5 flex justify-between items-center shadow-sm">
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

    <main class="flex-1 p-8 max-w-7xl mx-auto w-full space-y-8">
        <div class="grid grid-cols-1 md:grid-cols-4 gap-6">
            <div class="glass-card p-6 rounded-2xl">
                <div class="text-slate-400 text-xs font-bold uppercase tracking-wider mb-2 flex items-center"><i class="fas fa-coins mr-2 text-slate-500"></i>Settled Revenue</div>
                <div class="text-3xl font-black text-white mt-1"><span id="stat-revenue">--</span> <span class="text-sm font-medium text-slate-500">sats</span></div>
            </div>
            <div class="glass-card p-6 rounded-2xl">
                <div class="text-slate-400 text-xs font-bold uppercase tracking-wider mb-2 flex items-center"><i class="fas fa-file-invoice mr-2 text-slate-500"></i>Invoices Issued</div>
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

        refresh();
        setInterval(refresh, 8000);
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_admin)], include_in_schema=False)
def serve_dashboard():
    return DASHBOARD_HTML
