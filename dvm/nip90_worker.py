"""AurAgrid NIP-90 data vending machine (DVM).

Answers Nostr job requests for AurAgrid feeds, and only those:
  * job requests of kind AURAGRID_DVM_JOB_KIND (default 5990) that name a feed,
  * or any 5000-5999 job request that tags this DVM's pubkey ("p" tag).
Every other job on the relays is ignored, and jobs older than startup are
never answered (subscriptions use "since").

A job names its feed with ["param", "feed", "<feed>"] or ["i", "<feed>", "text"].
Feed names come from the live manifest: e.g. maritime-weather, energy/grid,
spr-stockpile, lng-supply-chain, ercot-lmp.

  free + live  -> job result (kind = request kind + 1000): the feed's latest data,
                  lists cut to 10 items to fit relay size limits, plus the URL of
                  the full free endpoint
  paid + live  -> feedback "payment-required" with the L402 URL and the current
                  quote (the same price Aperture charges)
  otherwise    -> feedback "error" (unknown feed / coming soon)

The catalog and prices are read from the backend's /agent-manifest.json and
/api/v1/price quotes, so the DVM never drifts from pricing.yaml.

Environment
-----------
AURAGRID_DVM_KEY_FILE      hex private key (default: dvm/dvm_key.txt next to this file)
AURAGRID_DVM_RELAYS        comma-separated relay URLs
AURAGRID_DVM_JOB_KIND      default 5990
AURAGRID_DVM_JOBS_PER_HOUR per requesting pubkey (default 20)
AURAGRID_BACKEND_URL       backend for manifest/data/quotes (default http://127.0.0.1:8000)
AURAGRID_PUBLIC_URL        public base URL put in results (default https://84.12.79.124)
"""
import asyncio
import json
import logging
import os
import sys
import time
from collections import defaultdict, deque

import httpx
import websockets
from nostr.event import Event
from nostr.key import PrivateKey

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from samples import truncate  # noqa: E402

log = logging.getLogger("auragrid.dvm")

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.environ.get("AURAGRID_DVM_KEY_FILE", os.path.join(HERE, "dvm_key.txt"))
RELAYS = [r.strip() for r in os.environ.get(
    "AURAGRID_DVM_RELAYS", "wss://relay.damus.io,wss://nos.lol,wss://relay.primal.net").split(",") if r.strip()]
JOB_KIND = int(os.environ.get("AURAGRID_DVM_JOB_KIND", "5990"))
JOBS_PER_HOUR = int(os.environ.get("AURAGRID_DVM_JOBS_PER_HOUR", "20"))
BACKEND = os.environ.get("AURAGRID_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
PUBLIC = os.environ.get("AURAGRID_PUBLIC_URL", "https://84.12.79.124").rstrip("/")
MAX_RESULT_BYTES = 60_000      # most relays reject events much larger than ~64 KB
MANIFEST_TTL = 300


# ---------------------------------------------------------------------------
# Pure logic (tested in tests/test_dvm.py)
# ---------------------------------------------------------------------------
def feed_key(path: str) -> str:
    """'/api/v1/batch/maritime-weather' -> 'maritime-weather'; '/api/v1/energy/grid' -> 'energy/grid'."""
    p = path[len("/api/v1/"):] if path.startswith("/api/v1/") else path.strip("/")
    head, _, rest = p.partition("/")
    return rest if head in ("batch", "wholesale") and rest else p


def catalog_from_manifest(manifest: dict) -> dict:
    """feed key (and aliases with underscores) -> manifest endpoint entry."""
    out = {}
    for entry in manifest.get("endpoints", []):
        key = feed_key(entry["path"])
        out[key] = entry
        out[key.replace("-", "_")] = entry
        out[key.split("/")[-1]] = out.get(key.split("/")[-1], entry)
    return out


def requested_feed(event: dict):
    for tag in event.get("tags", []):
        if len(tag) >= 3 and tag[0] == "param" and tag[1] == "feed":
            return tag[2].strip().lower()
    for tag in event.get("tags", []):
        if len(tag) >= 2 and tag[0] == "i":
            return tag[1].strip().lower()
    return None


def addressed_to_us(event: dict, our_pubkey: str) -> bool:
    """Our job kind, or any job request that p-tags us. Everything else is not ours to answer."""
    kind = event.get("kind")
    if not isinstance(kind, int) or not 5000 <= kind < 6000:
        return False
    if kind == JOB_KIND:
        return True
    return any(len(t) >= 2 and t[0] == "p" and t[1] == our_pubkey for t in event.get("tags", []))


def plan_reply(event: dict, catalog: dict) -> dict:
    """What to answer: {'action': 'result'|'payment'|'error', ...}."""
    feed = requested_feed(event)
    if not feed:
        return {"action": "error", "message": "Name a feed: [\"param\", \"feed\", \"<feed>\"]. Feeds: %s"
                % ", ".join(sorted({feed_key(e["path"]) for e in catalog.values()}))}
    entry = catalog.get(feed)
    if entry is None:
        return {"action": "error", "message": "Unknown feed %r" % feed}
    if entry.get("status") != "live":
        return {"action": "error", "message": "%s is not available yet (coming soon)" % entry["name"]}
    if entry.get("access") == "free":
        return {"action": "result", "entry": entry}
    return {"action": "payment", "entry": entry}


def compact_result(entry: dict, body: dict) -> str:
    """Relay-sized JSON for a job result: lists cut to 10 items, then to 3 if still too big."""
    for limit in (10, 3):
        data, cut = truncate(body.get("data"), limit)
        content = json.dumps({
            "feed": feed_key(entry["path"]), "name": entry["name"],
            "full_data_url": PUBLIC + entry["path"], "access": "free",
            "served_at": body.get("served_at"), "lists_truncated": cut, "data": data,
        }, separators=(",", ":"))
        if len(content.encode()) <= MAX_RESULT_BYTES:
            return content
    return json.dumps({"feed": feed_key(entry["path"]), "name": entry["name"],
                       "full_data_url": PUBLIC + entry["path"], "access": "free",
                       "note": "Result too large for a Nostr event; fetch full_data_url (free)."})


class RateLimiter:
    def __init__(self, per_hour: int, clock=time.time):
        self.per_hour, self.clock, self.seen = per_hour, clock, defaultdict(deque)

    def allow(self, pubkey: str) -> bool:
        now, q = self.clock(), self.seen[pubkey]
        while q and now - q[0] > 3600:
            q.popleft()
        if len(q) >= self.per_hour:
            return False
        q.append(now)
        return True


# ---------------------------------------------------------------------------
# Nostr plumbing
# ---------------------------------------------------------------------------
def load_key() -> PrivateKey:
    if os.path.exists(KEY_FILE):
        with open(KEY_FILE) as fh:
            return PrivateKey(bytes.fromhex(fh.read().strip()))
    key = PrivateKey()
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(key.hex())
    log.warning("Created a new DVM identity in %s", KEY_FILE)
    return key


def signed(key: PrivateKey, kind: int, content: str, tags: list) -> dict:
    evt = Event(public_key=key.public_key.hex(), content=content, kind=kind, tags=tags,
                created_at=int(time.time()))
    key.sign_event(evt)
    return {"id": evt.id, "pubkey": evt.public_key, "created_at": evt.created_at, "kind": evt.kind,
            "tags": evt.tags, "content": evt.content, "sig": evt.signature}


def announcement(key: PrivateKey, manifest: dict) -> dict:
    """NIP-89 handler information (kind 31990) for this DVM."""
    feeds = sorted({feed_key(e["path"]) for e in manifest.get("endpoints", []) if e.get("status") == "live"})
    content = json.dumps({
        "name": "AurAgrid Data DVM",
        "about": "Energy and maritime data from public sources (U.S. EIA, NOAA, ERCOT, IMF PortWatch). "
                 "Send kind %d with [\"param\",\"feed\",\"<feed>\"]. Free feeds are answered directly; "
                 "paid feeds return an L402 link and price. Live feeds: %s." % (JOB_KIND, ", ".join(feeds) or "none"),
        "website": PUBLIC + "/llms.txt",
    })
    tags = [["d", "auragrid-data-dvm"], ["k", str(JOB_KIND)]] + [["t", f] for f in feeds]
    return signed(key, 31990, content, tags)


class DVM:
    def __init__(self, key: PrivateKey):
        self.key, self.pub = key, key.public_key.hex()
        self.limiter = RateLimiter(JOBS_PER_HOUR)
        self.handled, self._manifest, self._manifest_at = {}, None, 0.0
        self.http = httpx.AsyncClient(timeout=15)

    async def manifest(self) -> dict:
        if self._manifest is None or time.time() - self._manifest_at > MANIFEST_TTL:
            r = await self.http.get(BACKEND + "/agent-manifest.json")
            r.raise_for_status()
            self._manifest, self._manifest_at = r.json(), time.time()
        return self._manifest

    def feedback(self, job: dict, status: str, message: str, extra_tags=()) -> dict:
        tags = [["status", status, message], ["e", job["id"]], ["p", job["pubkey"]], *extra_tags]
        return signed(self.key, 7000, json.dumps({"message": message}), tags)

    async def reply(self, job: dict):
        """The event to publish for a job, or None to stay silent."""
        if not addressed_to_us(job, self.pub) or job.get("id") in self.handled:
            return None
        self.handled[job["id"]] = time.time()
        if len(self.handled) > 10000:
            cutoff = time.time() - 3600
            self.handled = {k: v for k, v in self.handled.items() if v > cutoff}
        if not self.limiter.allow(job["pubkey"]):
            return self.feedback(job, "error", "Rate limit: %d jobs per hour per pubkey" % JOBS_PER_HOUR)

        plan = plan_reply(job, catalog_from_manifest(await self.manifest()))
        if plan["action"] == "error":
            return self.feedback(job, "error", plan["message"])
        entry = plan["entry"]
        if plan["action"] == "payment":
            q = (await self.http.get(BACKEND + "/api/v1/price" + entry["path"][len("/api/v1"):])).json()
            msg = "Paid feed: GET %s with L402 (pay the invoice in the 402 response). Current price %s sats." % (
                PUBLIC + entry["path"], q.get("price_sats"))
            return self.feedback(job, "payment-required", msg, [["amount", str(int(q["price_sats"]) * 1000)]])
        r = await self.http.get(BACKEND + entry["path"])
        if r.status_code != 200:
            return self.feedback(job, "error", "%s is temporarily unavailable (HTTP %d)" % (entry["name"], r.status_code))
        tags = [["request", json.dumps(job)], ["e", job["id"]], ["p", job["pubkey"]]]
        tags += [t for t in job.get("tags", []) if t and t[0] == "i"]
        log.info("Delivered %s to %s (job %s)", entry["name"], job["pubkey"][:8], job["id"][:8])
        return signed(self.key, job["kind"] + 1000, compact_result(entry, r.json()), tags)

    async def run_relay(self, uri: str):
        while True:
            try:
                async with websockets.connect(uri, max_size=2 ** 20) as ws:
                    since = int(time.time())
                    await ws.send(json.dumps(["EVENT", announcement(self.key, await self.manifest())]))
                    await ws.send(json.dumps(["REQ", "auragrid-jobs",
                                              {"kinds": [JOB_KIND], "since": since},
                                              {"kinds": list(range(5000, 6000)), "#p": [self.pub], "since": since}]))
                    log.info("Listening on %s", uri)
                    async for message in ws:
                        data = json.loads(message)
                        if isinstance(data, list) and len(data) >= 3 and data[0] == "OK" and data[2] is False:
                            log.warning("%s rejected event %s: %s", uri, str(data[1])[:8],
                                        data[3] if len(data) > 3 else "")
                            continue
                        if isinstance(data, list) and len(data) >= 3 and data[0] == "EVENT":
                            try:
                                out = await self.reply(data[2])
                            except Exception:
                                log.exception("Job handling failed")
                                continue
                            if out:
                                await ws.send(json.dumps(["EVENT", out]))
            except Exception as exc:
                log.warning("Relay %s: %s; reconnecting in 30s", uri, exc)
                await asyncio.sleep(30)


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    dvm = DVM(load_key())
    log.info("AurAgrid DVM %s, job kind %d, relays: %s", dvm.key.public_key.bech32(), JOB_KIND, ", ".join(RELAYS))
    await asyncio.gather(*(dvm.run_relay(r) for r in RELAYS))


if __name__ == "__main__":
    asyncio.run(main())
