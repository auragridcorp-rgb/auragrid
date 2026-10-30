"""AurAgrid pricer: USD-pegged sat prices for Aperture's dynamic pricing.

Two parts in one file:

* PriceEngine: keeps a cached, sanity-checked BTC/USD price (median of several
  public feeds, refreshed in the background) and turns USD targets from
  pricing.yaml into sat prices. Importable by main.py for the /api/v1/price quote.
* gRPC server (run `python pricer.py`): answers Aperture's GetPrice call so the
  invoice Aperture issues at the 402 matches the quote.

Only endpoints listed in pricing.yaml (and single-feed plans under `plans:`)
are USD-pegged. Everything else keeps its
static price from aperture.yaml. If the BTC feed is unavailable for too long,
USD-pegged endpoints also fall back to their static aperture.yaml price.

Setup
-----
    pip install grpcio grpcio-tools httpx pyyaml
    # Use the .proto shipped with YOUR Aperture version (pricesrpc directory of
    # the aperture source). pricesrpc.proto next to this file is only a fallback.
    python -m grpc_tools.protoc -I <dir-with-proto> --python_out=. \
        --grpc_python_out=. <dir-with-proto>/<file>.proto
    python pricer.py

Environment (all optional)
--------------------------
AURAGRID_APERTURE_CONFIG   aperture.yaml path            (default: aperture.yaml)
AURAGRID_PRICING_CONFIG    USD targets path              (default: pricing.yaml)
AURAGRID_PRICER_ADDR       gRPC listen address           (default: 127.0.0.1:10010)
AURAGRID_BTC_REFRESH       feed refresh seconds          (default: 30)
AURAGRID_BTC_FRESH         price counts as fresh for (s) (default: 120)
AURAGRID_BTC_MAX_STALE     last-good usable up to (s)    (default: 3600)
AURAGRID_MIN_SATS          price floor                   (default: 1)
AURAGRID_MAX_SATS          price ceiling / sanity guard  (default: 2000000)
"""
import logging
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("auragrid.pricer")

REFRESH_SECONDS = float(os.environ.get("AURAGRID_BTC_REFRESH", "30"))
FRESH_SECONDS = float(os.environ.get("AURAGRID_BTC_FRESH", "120"))
MAX_STALE_SECONDS = float(os.environ.get("AURAGRID_BTC_MAX_STALE", "3600"))
MIN_SATS = int(os.environ.get("AURAGRID_MIN_SATS", "1"))
MAX_SATS = int(os.environ.get("AURAGRID_MAX_SATS", "2000000"))

BTC_USD_MIN, BTC_USD_MAX = 1_000.0, 10_000_000.0   # reject absurd feed values
OUTLIER_PCT = 0.05            # drop a source more than 5% from the median
SINGLE_SOURCE_MAX_MOVE = 0.10 # a lone source must be within 10% of last good
PRICE_SIG_DIGITS = 3          # round sats to 3 significant digits (stable quotes)
QUOTE_VALIDITY_SECONDS = 60
QUOTE_TOLERANCE_PCT = 3       # clients should accept an invoice within this % of a quote


# ---------------------------------------------------------------------------
# BTC/USD feeds
# ---------------------------------------------------------------------------
def _mempool(client) -> float:
    return float(client.get("https://mempool.space/api/v1/prices").json()["USD"])


def _coinbase(client) -> float:
    return float(client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot").json()["data"]["amount"])


def _kraken(client) -> float:
    data = client.get("https://api.kraken.com/0/public/Ticker", params={"pair": "XBTUSD"}).json()
    return float(next(iter(data["result"].values()))["c"][0])


FEEDS = {"mempool.space": _mempool, "coinbase": _coinbase, "kraken": _kraken}


def fetch_all(feeds=None) -> dict:
    """Query every feed in parallel; failures are logged and skipped."""
    import httpx  # imported lazily so the module loads without it

    feeds = feeds or FEEDS
    out = {}
    with httpx.Client(timeout=3.0, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=len(feeds)) as pool:
            futures = {name: pool.submit(fn, client) for name, fn in feeds.items()}
            for name, fut in futures.items():
                try:
                    out[name] = float(fut.result())
                except Exception as exc:
                    log.warning("BTC feed %s failed: %s", name, exc)
    return out


def aggregate(quotes: list, last_good: Optional[float] = None) -> Optional[float]:
    """Combine feed quotes into one BTC/USD price, or None if they can't be trusted."""
    valid = sorted(q for q in quotes if BTC_USD_MIN <= q <= BTC_USD_MAX)
    if not valid:
        return None
    if len(valid) == 1:
        only = valid[0]
        if last_good and abs(only / last_good - 1) > SINGLE_SOURCE_MAX_MOVE:
            return None  # one source alone must not move the price sharply
        return only
    median = statistics.median(valid)
    kept = [q for q in valid if abs(q / median - 1) <= OUTLIER_PCT]
    return statistics.median(kept) if kept else None


def round_sig(n: int, digits: int = PRICE_SIG_DIGITS) -> int:
    if n < 10 ** digits:
        return n
    step = 10 ** (len(str(n)) - digits)
    return int(round(n / step) * step)


# ---------------------------------------------------------------------------
# Path handling and config
# ---------------------------------------------------------------------------
def normalize_path(path: str) -> str:
    """'/api/v1/wholesale/ercot-lmp?x=1' -> 'ercot-lmp'; '/api/v1/esg/carbon' -> 'esg/carbon'."""
    p = (path or "").split("?", 1)[0].strip()
    if p.startswith("/api/v1/"):
        p = p[len("/api/v1/"):]
    p = p.strip("/")
    head, _, rest = p.partition("/")
    if head in ("batch", "wholesale") and rest:
        p = rest
    return p


def _load_yaml(path: str):
    import yaml

    with open(path) as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------
class PriceEngine:
    def __init__(self, aperture_config="aperture.yaml", pricing_config="pricing.yaml"):
        self.aperture_config = aperture_config
        self.pricing_config = pricing_config
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._btc = None            # last good BTC/USD
        self._btc_time = 0.0        # monotonic time of last good value
        self._rules = []            # [(compiled regex, aperture service dict)]
        self._usd = {}              # normalized key -> USD target
        self._mtimes = {}
        self.reload_config(force=True)

    # -- config -------------------------------------------------------------
    def reload_config(self, force=False):
        """Reload aperture.yaml / pricing.yaml when their files change."""
        for attr, path in (("aperture", self.aperture_config), ("pricing", self.pricing_config)):
            try:
                mtime = os.stat(path).st_mtime
            except OSError as exc:
                if force:
                    log.warning("Cannot read %s: %s", path, exc)
                continue
            if not force and self._mtimes.get(attr) == mtime:
                continue
            try:
                data = _load_yaml(path)
                if attr == "aperture":
                    rules = []
                    for svc in data.get("services") or []:
                        if svc.get("pathregexp"):
                            rules.append((re.compile(svc["pathregexp"]), svc))
                    self._rules = rules
                else:
                    usd = {}
                    for key, value in (data.get("endpoints") or {}).items():
                        if float(value) > 0:
                            usd[normalize_path(str(key))] = float(value)
                    # Single-feed plans (30-day passes) are USD-pegged too; bundles
                    # stay static (see pass_support.py for why).
                    from pass_support import PlanRouter, parse_plans
                    usd.update(PlanRouter(parse_plans(data.get("plans"))).usd_targets())
                    self._usd = usd
                self._mtimes[attr] = mtime
                log.info("Loaded %s", path)
            except Exception:
                log.exception("Failed to load %s; keeping previous config", path)

    def _static_rule(self, path: str) -> Optional[dict]:
        for rx, svc in self._rules:
            if rx.match(path):
                return svc
        return None

    def _usd_target(self, path: str) -> Optional[float]:
        key = normalize_path(path)
        best = None
        for k, usd in self._usd.items():
            if key == k or key.startswith(k + "/"):
                if best is None or len(k) > len(best[0]):
                    best = (k, usd)
        return best[1] if best else None

    # -- BTC price ------------------------------------------------------------
    def refresh(self) -> bool:
        quotes = fetch_all()
        price = aggregate(list(quotes.values()), self._btc)
        if price is None:
            log.warning("No trustworthy BTC/USD price this round (got %s)", quotes)
            return False
        with self._lock:
            self._btc, self._btc_time = price, time.monotonic()
        log.info("BTC/USD %.2f from %s", price, ", ".join(sorted(quotes)))
        return True

    def start(self):
        self.refresh()
        self._thread = threading.Thread(target=self._loop, name="btc-refresh", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.wait(REFRESH_SECONDS):
            try:
                self.reload_config()
                self.refresh()
            except Exception:
                log.exception("Refresh loop error")

    def btc_state(self):
        """(price, age_seconds, state) where state is fresh | stale | unavailable."""
        with self._lock:
            price, t = self._btc, self._btc_time
        if price is None:
            return None, None, "unavailable"
        age = time.monotonic() - t
        if age <= FRESH_SECONDS:
            return price, age, "fresh"
        if age <= MAX_STALE_SECONDS:
            return price, age, "stale"
        return None, age, "unavailable"

    # -- pricing ----------------------------------------------------------------
    def _price(self, path: str):
        rule = self._static_rule(path)
        static = int(rule["price"]) if rule and rule.get("price") is not None else None
        usd = self._usd_target(path)
        btc, age, state = self.btc_state()

        if usd is not None and btc is not None:
            sats = round_sig(max(MIN_SATS, round(usd / btc * 100_000_000)))
            if sats <= MAX_SATS:
                return {"sats": sats, "mode": "usd_pegged", "usd": usd, "btc": btc,
                        "age": age, "state": state, "rule": rule}
            log.error("Computed %d sats for %s exceeds ceiling %d; using static price", sats, path, MAX_SATS)
        if static is not None:
            return {"sats": static, "mode": "static", "usd": usd, "btc": btc,
                    "age": age, "state": state, "rule": rule}
        return None

    def sats_for(self, path: str) -> Optional[int]:
        """Price Aperture should charge for this request path (None = unpriced)."""
        result = self._price(path)
        return result["sats"] if result else None

    def quote(self, path: str) -> Optional[dict]:
        """Machine-readable quote for the HTTP /api/v1/price route."""
        r = self._price(path)
        if r is None:
            return None
        out = {
            "path": path,
            "price_sats": r["sats"],
            "pricing": r["mode"],
            "quoted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "valid_for_seconds": QUOTE_VALIDITY_SECONDS,
            "tolerance_pct": QUOTE_TOLERANCE_PCT,
            "stale": r["state"] != "fresh",
        }
        if r["mode"] == "usd_pegged":
            out["usd_price"] = r["usd"]
            out["btc_usd"] = round(r["btc"], 2)
        if r["rule"] and r["rule"].get("timeout"):
            out["token_ttl_seconds"] = int(r["rule"]["timeout"])
        return out


# ---------------------------------------------------------------------------
# gRPC server for Aperture's dynamic pricing
# ---------------------------------------------------------------------------
def build_server(engine: PriceEngine, address: str):
    import grpc
    import pricesrpc_pb2 as pb              # generated from Aperture's own .proto
    import pricesrpc_pb2_grpc as pb_grpc

    class Pricer(pb_grpc.PricesServicer):
        def GetPrice(self, request, context):
            sats = engine.sats_for(request.path)
            if sats is None:
                log.error("No price for path %r", request.path)
                context.abort(grpc.StatusCode.NOT_FOUND, "no price for path")
            log.info("GetPrice %s -> %d sats", request.path, sats)
            return pb.GetPriceResponse(price_sats=sats)

    from concurrent import futures

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_PricesServicer_to_server(Pricer(), server)
    server.add_insecure_port(address)  # keep this on loopback
    return server


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    engine = PriceEngine(
        os.environ.get("AURAGRID_APERTURE_CONFIG", "aperture.yaml"),
        os.environ.get("AURAGRID_PRICING_CONFIG", "pricing.yaml"),
    )
    engine.start()
    address = os.environ.get("AURAGRID_PRICER_ADDR", "127.0.0.1:10010")
    server = build_server(engine, address)
    server.start()
    log.info("AurAgrid pricer listening on %s", address)
    try:
        server.wait_for_termination()
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
