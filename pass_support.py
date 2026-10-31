"""30-day passes and multi-feed bundles on top of the hourly L402 feeds.

A plan is a separate Aperture service with its own path prefix, price and
token lifetime. Aperture sells and validates the token; the backend then
rewrites the plan path onto the canonical feed route, so passes and bundles
serve exactly the same data as the hourly endpoints:

    /api/v1/pro/ercot-lmp              -> /api/v1/<batch>/ercot-lmp
    /api/v1/bundle/lng-supply-chain    -> /api/v1/<batch>/lng-supply-chain

Plans live in pricing.yaml under `plans:` (one source of truth for
gen_aperture.py, pricer.py, the ledger and the app):

    plans:
      ercot-pro-pass:
        kind: pass_30d
        prefix: pro
        feeds: [ercot-lmp]
        usd: 49.00
        timeout: 2592000

Pricing rule (forced by Aperture): with dynamicprice enabled, Aperture scopes
a token to service name + exact path, so a dynamically priced token could
never open more than one feed. Single-feed plans are therefore USD-pegged via
the pricer; multi-feed bundles get a static sat price that gen_aperture.py
computes from `usd` at generation time.

Only the original request path decides the plan. The ledger and attestation
middlewares must wrap PlanRewriteMiddleware (be added after it) so they see
the path the client paid for; raw_path is never rewritten, so v2 signatures
cover the URL the client actually requested.
"""
import logging
import re
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("auragrid.plans")

HOURLY = "hourly"
PLAN_KINDS = (HOURLY, "pass_30d", "bundle")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class Plan:
    name: str            # Aperture service name, e.g. "ercot-pro-pass"
    kind: str            # "pass_30d" | "bundle"
    prefix: str          # URL segment under /api/v1/, e.g. "pro"
    feeds: tuple         # feed slugs, e.g. ("ercot-lmp",)
    timeout: int         # token lifetime in seconds
    usd: Optional[float] = None
    sats: Optional[int] = None   # static price; required for multi-feed plans
    title: str = ""

    @property
    def dynamic(self) -> bool:
        """USD-pegged through the pricer (only possible for single-feed plans)."""
        return len(self.feeds) == 1 and self.usd is not None

    @property
    def pathregexp(self) -> str:
        # prefix and feeds are validated slugs ([a-z0-9-]), so no escaping is needed
        if len(self.feeds) == 1:
            return "^/api/v1/%s/%s$" % (self.prefix, self.feeds[0])
        return "^/api/v1/%s/(%s)$" % (self.prefix, "|".join(self.feeds))

    def paths(self) -> list:
        return ["/api/v1/%s/%s" % (self.prefix, f) for f in self.feeds]

    def display_name(self) -> str:
        return self.title or self.name


def load_plans(pricing_config: str = "pricing.yaml") -> list:
    """Parse and validate `plans:` from pricing.yaml. Invalid plans are skipped."""
    try:
        import yaml
        with open(pricing_config) as fh:
            raw = (yaml.safe_load(fh) or {}).get("plans") or {}
    except FileNotFoundError:
        return []
    return parse_plans(raw)


def parse_plans(raw: dict) -> list:
    plans, prefixes = [], set()
    for name, spec in (raw or {}).items():
        if spec.get("enabled") is False:
            continue   # kept in pricing.yaml for later, not sold now
        try:
            plan = Plan(
                name=str(name),
                kind=str(spec["kind"]),
                prefix=str(spec["prefix"]),
                feeds=tuple(str(f) for f in spec["feeds"]),
                timeout=int(spec["timeout"]),
                usd=float(spec["usd"]) if spec.get("usd") is not None else None,
                sats=int(spec["sats"]) if spec.get("sats") is not None else None,
                title=str(spec.get("title") or ""),
            )
            if plan.kind not in PLAN_KINDS or plan.kind == HOURLY:
                raise ValueError("kind must be pass_30d or bundle")
            if not _SLUG.match(plan.name) or not _SLUG.match(plan.prefix):
                raise ValueError("name and prefix must be lowercase slugs")
            if plan.prefix in ("batch", "wholesale", "price", "sample"):
                raise ValueError("prefix %r collides with an existing route" % plan.prefix)
            if plan.prefix in prefixes:
                raise ValueError("prefix %r used by two plans" % plan.prefix)
            if not plan.feeds or not all(_SLUG.match(f) for f in plan.feeds):
                raise ValueError("feeds must be a non-empty list of slugs")
            if plan.timeout <= 0:
                raise ValueError("timeout must be a positive number of seconds")
            if plan.usd is None and plan.sats is None:
                raise ValueError("needs usd or sats")
        except Exception as exc:
            log.error("Ignoring invalid plan %r in pricing.yaml: %s", name, exc)
            continue
        prefixes.add(plan.prefix)
        plans.append(plan)
    return plans


@dataclass(frozen=True)
class Access:
    """Which feeds are offered, from pricing.yaml `access:`. Feeds in neither list
    are not offered at all (no route, not in the manifest, not sold by Aperture)."""
    free: tuple = ()          # served without payment, rate-limited per client IP
    paid: tuple = ()          # hourly L402 tokens
    retired: tuple = ()       # answered with 410 Gone
    retired_prefixes: tuple = ()   # e.g. "sample" -> everything under /api/v1/sample/ is 410
    requests_per_minute: int = 30
    burst: int = 10

    @property
    def offered(self) -> set:
        return set(self.free) | set(self.paid)


def feed_path(key: str, batch_prefix: str = "batch") -> str:
    """'ercot-lmp' -> '/api/v1/batch/ercot-lmp'; 'esg/carbon' -> '/api/v1/esg/carbon'."""
    return "/api/v1/" + (key if "/" in key else "%s/%s" % (batch_prefix, key))


def load_access(pricing_config: str = "pricing.yaml") -> Access:
    try:
        import yaml
        with open(pricing_config) as fh:
            raw = (yaml.safe_load(fh) or {}).get("access") or {}
    except FileNotFoundError:
        raw = {}
    free = raw.get("free") or {}
    paid = raw.get("paid") or {}
    retired = raw.get("retired") or {}
    access = Access(free=tuple(free.get("feeds") or ()), paid=tuple(paid.get("feeds") or ()),
                    retired=tuple(retired.get("feeds") or ()), retired_prefixes=tuple(retired.get("prefixes") or ()),
                    requests_per_minute=int(free.get("requests_per_minute", 30)),
                    burst=int(free.get("burst", 10)))
    for a, b in ((access.free, access.paid), (access.free, access.retired), (access.paid, access.retired)):
        both = set(a) & set(b)
        if both:
            raise ValueError("feeds listed twice in %s access: %s" % (pricing_config, sorted(both)))
    return access


class PlanRouter:
    def __init__(self, plans: list, feed_route_prefix: str = "batch"):
        self.plans = list(plans)
        self.by_prefix = {p.prefix: p for p in self.plans}
        self.feed_route_prefix = feed_route_prefix

    def match(self, path: str):
        """(plan, feed) for '/api/v1/<prefix>/<feed>', (plan, None) if the feed is
        not in the plan, or (None, None) if the path is not a plan path."""
        if not path.startswith("/api/v1/"):
            return None, None
        prefix, _, feed = path[len("/api/v1/"):].partition("/")
        plan = self.by_prefix.get(prefix)
        if plan is None:
            return None, None
        return plan, (feed if feed in plan.feeds else None)

    def kind_for_path(self, path: str) -> str:
        plan, _ = self.match(path)
        return plan.kind if plan else HOURLY

    def plan_for_path(self, path: str) -> Optional[Plan]:
        return self.match(path)[0]

    def usd_targets(self) -> dict:
        """pricer keys ('pro/ercot-lmp') -> USD for USD-pegged (single-feed) plans."""
        return {"%s/%s" % (p.prefix, p.feeds[0]): p.usd for p in self.plans if p.dynamic}


class PlanRewriteMiddleware:
    """Pure-ASGI: rewrite plan paths onto canonical feed routes.

    Runs only after Aperture has accepted the plan token (the backend must not
    be reachable except through Aperture; see AURAGRID_GATEWAY_SECRET in main).
    The original path is kept in scope["state"]["auragrid_plan_path"].
    """

    def __init__(self, app, router: PlanRouter):
        self.app, self.router = app, router

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        plan, feed = self.router.match(path)
        if plan is None:
            await self.app(scope, receive, send)
            return
        if feed is None:
            await _json(send, 404, b'{"detail":"Feed not included in this plan"}')
            return
        scope = dict(scope)
        state = dict(scope.get("state") or {})
        state.update(auragrid_plan=plan.name, auragrid_plan_kind=plan.kind, auragrid_plan_path=path)
        scope["state"] = state
        scope["path"] = "/api/v1/%s/%s" % (self.router.feed_route_prefix, feed)
        await self.app(scope, receive, send)


async def _json(send, status: int, body: bytes):
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
