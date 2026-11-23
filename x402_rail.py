"""x402: a second payment rail (USDC on Base) next to L402, for the same paid feeds.

    GET /x402/v1/<batch>/<feed>    e.g. /x402/v1/batch/btc-fees

Off unless configured (environment, set by the owner; nothing here holds keys):
    AURAGRID_X402_PAY_TO           EVM address that receives the USDC (required)
    AURAGRID_X402_FACILITATOR_URL  facilitator that verifies and settles payments (required)
    AURAGRID_X402_NETWORK          CAIP-2 network, default eip155:8453 (Base mainnet)
Prices are per request, in USD, from pricing.yaml `x402:` (only feeds listed there are sold).

Order of checks for one request (see `gate`):
  1. not configured / not an x402 feed      -> 404, no payment asked
  2. feed stale (jobs.feed_degraded)        -> 503 stale refusal, NO payment requirement issued
  3. x402 middleware: no/invalid payment    -> 402 with PAYMENT-REQUIRED (price, payTo, network)
  4. paid: same feed handler as L402; the facilitator settles only after a 2xx response
  5. settled: recorded in the ledger (x402_payments), PAYMENT-RESPONSE header returned

The same freshness rule as L402: nobody pays for stale data on either rail. The x402 library
settles only responses below HTTP 400, so a feed that turns stale between the check and the
handler (503) is not charged either.
"""
import base64
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

log = logging.getLogger("auragrid.x402")

PREFIX = "/x402/v1/"
BASE_MAINNET = "eip155:8453"
TESTNETS = {"eip155:84532"}          # Base Sepolia: the only network x402.org's free facilitator covers
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
URL_RE = re.compile(r"^https://[^\s/]+(/[^\s]*)?$")


@dataclass
class Config:
    pay_to: str
    facilitator_url: str
    network: str = BASE_MAINNET
    prices: dict = field(default_factory=dict)   # feed key (e.g. "btc-fees") -> USD per request
    batch_prefix: str = "batch"

    def path(self, key: str) -> str:
        return "%s%s/%s" % (PREFIX, self.batch_prefix, key)

    def price_str(self, key: str) -> str:
        return "$%s" % format(self.prices[key], "f").rstrip("0").rstrip(".")


def load_config(prices: dict, env=os.environ, batch_prefix: str = "batch") -> Optional[Config]:
    """The x402 config, or None (rail off) when anything required is missing or malformed."""
    pay_to = (env.get("AURAGRID_X402_PAY_TO") or "").strip()
    url = (env.get("AURAGRID_X402_FACILITATOR_URL") or "").strip().rstrip("/")
    network = (env.get("AURAGRID_X402_NETWORK") or BASE_MAINNET).strip()
    if not pay_to and not url:
        return None
    problems = []
    if not ADDRESS_RE.match(pay_to):
        problems.append("AURAGRID_X402_PAY_TO is not a 0x... EVM address")
    if not URL_RE.match(url):
        problems.append("AURAGRID_X402_FACILITATOR_URL must be an https:// URL")
    if not re.match(r"^eip155:\d+$", network):
        problems.append("AURAGRID_X402_NETWORK must look like eip155:<chain id>")
    if url.startswith("https://x402.org") and network not in TESTNETS:
        problems.append("x402.org's facilitator is testnet-only; it cannot settle %s" % network)
    clean = {}
    for key, usd in (prices or {}).items():
        try:
            usd = float(usd)
        except (TypeError, ValueError):
            usd = 0.0
        if usd > 0:
            clean[key] = usd
    if not clean:
        problems.append("pricing.yaml has no positive x402: prices")
    if problems:
        log.error("x402 rail OFF: %s", "; ".join(problems))
        return None
    return Config(pay_to=pay_to, facilitator_url=url, network=network, prices=clean, batch_prefix=batch_prefix)


def feed_key(path: str, batch_prefix: str = "batch") -> Optional[str]:
    """'/x402/v1/batch/btc-fees' -> 'btc-fees' (None for anything else)."""
    m = re.match(r"^%s%s/([a-z0-9-]+)/?$" % (re.escape(PREFIX), re.escape(batch_prefix)), path)
    return m.group(1) if m else None


def routes_config(cfg: Config, describe: Callable[[str], dict]) -> dict:
    """Route table for the x402 middleware, one exact-price route per sold feed."""
    routes = {}
    for key in sorted(cfg.prices):
        info = describe(key) or {}
        routes["GET " + cfg.path(key)] = {
            "accepts": {"scheme": "exact", "payTo": cfg.pay_to, "price": cfg.price_str(key),
                        "network": cfg.network},
            "description": (info.get("description") or info.get("name") or key)[:500],
            "mimeType": "application/json",
        }
    return routes


def decode_payment_response(header: str) -> dict:
    """The PAYMENT-RESPONSE header (base64 JSON settle result), or {} if unreadable."""
    try:
        data = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def build_middleware(cfg: Config, describe: Callable[[str], dict], facilitator=None):
    """The library's FastAPI middleware for our routes (imported lazily: the rail is optional)."""
    from x402 import x402ResourceServer
    from x402.http import FacilitatorConfig, HTTPFacilitatorClient
    from x402.http.middleware.fastapi import payment_middleware
    from x402.mechanisms.evm.exact import register_exact_evm_server

    if facilitator is None:
        facilitator = HTTPFacilitatorClient(FacilitatorConfig(url=cfg.facilitator_url))
    server = register_exact_evm_server(x402ResourceServer(facilitator), cfg.network)
    return payment_middleware(routes_config(cfg, describe), server)


def install(app, cfg: Optional[Config], *, describe, degraded, stale_refusal, ledger,
            handler_for: Callable[[str], Callable], facilitator=None):
    """Register the gate middleware and the /x402/v1/<feed> routes on `app`.

    With cfg None the gate still answers /x402/* (404, "not enabled"), so the path is
    never served by anything else and the answer is explicit.
    """
    from fastapi.responses import JSONResponse

    mw = build_middleware(cfg, describe, facilitator) if cfg else None

    @app.middleware("http")
    async def x402_gate(request, call_next):
        path = request.url.path
        if not path.startswith(PREFIX):
            return await call_next(request)
        if cfg is None:
            return JSONResponse({"detail": "x402 payments are not enabled on this server; pay over L402 at "
                                           "/api/v1/... (see /agent-manifest.json)"}, status_code=404)
        key = feed_key(path, cfg.batch_prefix)
        if key is None or key not in cfg.prices:
            return JSONResponse({"detail": "No x402 product at this path",
                                 "sold": [cfg.path(k) for k in sorted(cfg.prices)]}, status_code=404)
        if request.method not in ("GET", "HEAD"):
            return JSONResponse({"detail": "Method not allowed"}, status_code=405)
        if degraded(key):
            return stale_refusal(key)           # before any payment requirement is issued
        request.state.auragrid_plan_kind = "x402"
        response = await mw(request, call_next)
        receipt = response.headers.get("payment-response")
        if receipt and response.status_code < 400:
            settled = decode_payment_response(receipt)
            if settled.get("success") and settled.get("transaction"):
                ledger.record_x402(settled["transaction"], key, settled.get("network") or cfg.network,
                                   settled.get("payer"), cfg.prices[key])
        return response

    if cfg:
        for key in sorted(cfg.prices):
            app.add_api_route(cfg.path(key), handler_for(key), methods=["GET"], name="x402-" + key,
                              include_in_schema=False)
        log.info("x402 rail ON: %s on %s, facilitator %s", ", ".join(cfg.path(k) for k in sorted(cfg.prices)),
                 cfg.network, cfg.facilitator_url)
    return mw
