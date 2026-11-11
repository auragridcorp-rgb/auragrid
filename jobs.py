"""What AurAgrid fetches, how often, and how the API reads it back.

The fetcher (fetcher.py) runs the jobs below and writes successful snapshots to
store.py; the API never calls an upstream source itself, it only reads the
store through StoreFeed. A failed fetch never overwrites the last good snapshot.

Freshness per feed:
  stale_after  older than this -> served with "stale": true (still the true as_of)
  max_age      older than this -> not served at all (503)
"""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional

import feeds

MIN, HOUR, DAY = 60, 3600, 86400


@dataclass(frozen=True)
class Job:
    key: str
    fetch: Callable[[], dict]
    interval: int
    stale_after: int
    max_age: int
    requires_env: Optional[str] = None   # job is skipped unless this variable is set

    def enabled(self) -> bool:
        return not self.requires_env or bool(os.environ.get(self.requires_env))


def _eia(fetch_fn):
    return lambda: fetch_fn(os.environ["EIA_API_KEY"])


def all_jobs() -> list:
    # Imported here so importing jobs.py stays cheap and side-effect free for the API.
    import btc_feed
    import eia_grid_feed
    import eia_lng_feed
    import eia_spr_feed
    import ln_feed
    import ndbc_feed
    import usgs_feed
    import requests

    ndbc = ndbc_feed.NDBCFetcher()
    return [
        Job("btc-fees", btc_feed.fetch, 1 * MIN, 15 * MIN, 6 * HOUR),
        Job("ln-network", ln_feed.fetch, 1 * HOUR, 3 * HOUR, 2 * DAY),
        Job("earthquakes", usgs_feed.fetch, 10 * MIN, 1 * HOUR, 1 * DAY),
        Job("maritime-weather", ndbc, 10 * MIN, 3 * HOUR, 12 * HOUR),
        Job("energy/grid", _eia(lambda k: eia_grid_feed.fetch(requests.Session(), k)), 15 * MIN, 6 * HOUR,
            2 * DAY, requires_env="EIA_API_KEY"),
        Job("spr-stockpile", _eia(lambda k: eia_spr_feed.fetch(requests.Session(), k)), 6 * HOUR, 10 * DAY,
            21 * DAY, requires_env="EIA_API_KEY"),
        Job("lng-supply-chain", _eia(eia_lng_feed.fetch), 12 * HOUR, 45 * DAY, 75 * DAY,
            requires_env="EIA_API_KEY"),
    ]


# Free copies of paid feeds: served from the same stored snapshots, just older.
DERIVED = {
    "btc-fees-delayed": {"source": "btc-fees", "delay": 10 * MIN},
    "ln-network-daily": {"source": "ln-network", "daily": True},
}

# Freshness limits by feed key (kept separate from all_jobs so the API doesn't import fetchers).
LIMITS = {
    "btc-fees": (15 * MIN, 6 * HOUR), "ln-network": (3 * HOUR, 2 * DAY), "earthquakes": (1 * HOUR, 1 * DAY),
    "maritime-weather": (3 * HOUR, 12 * HOUR), "energy/grid": (6 * HOUR, 2 * DAY),
    "spr-stockpile": (10 * DAY, 21 * DAY), "lng-supply-chain": (45 * DAY, 75 * DAY),
    "btc-fees-delayed": (30 * MIN, 6 * HOUR), "ln-network-daily": (2 * DAY, 3 * DAY),
}


def feed_degraded(store, key: str, clock=time.time) -> bool:
    """True when a feed would be served stale or not at all (503): never charge for it.

    Only feeds with freshness limits count; anything else is never "degraded".
    Uses fetch_status.last_success, which is when the newest snapshot was stored.
    """
    limits = LIMITS.get(key)
    if limits is None:
        return False
    status = store.status(key) or {}
    last_ok = status.get("last_success")
    return last_ok is None or clock() - last_ok > limits[0]


class StoreFeed:
    """API-side provider: the newest stored snapshot of a feed, never a live upstream call."""

    def __init__(self, store, key: str, stale_after: int, max_age: int, source: Optional[str] = None,
                 delay: int = 0, daily: bool = False, clock=time.time):
        self.store, self.key, self.source = store, key, source or key
        self.stale_after, self.max_age, self.delay, self.daily, self.clock = stale_after, max_age, delay, daily, clock

    def snapshot(self):
        if self.daily:   # yesterday's (or the last complete day's) final snapshot
            from store import Snapshot
            today = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
            done = [d for d in self.store.daily_history(self.source, limit=3) if d["day"] < today]
            return Snapshot(self.source, done[0]["data"], done[0]["as_of"], done[0]["retrieved_at"]) if done else None
        if self.delay:
            return self.store.latest_before(self.source, self.clock() - self.delay)
        return self.store.latest(self.source)

    def __call__(self) -> dict:
        snap = self.snapshot()
        if snap is None:
            raise feeds.FeedWarmingUp("%s: no snapshot stored yet" % self.key)
        age = self.clock() - snap.retrieved_at
        if age > self.max_age:
            raise feeds.FeedWarmingUp("%s: newest snapshot is %.0f h old, beyond its limit" % (self.key, age / 3600))
        return {**snap.data, "fetched_at": snap.retrieved_iso, "stale": age > self.stale_after}


def register_store_feeds(store, offered_keys) -> None:
    """Point every offered feed at the store (called by the API at startup)."""
    for key in offered_keys:
        if key not in LIMITS:
            continue
        stale_after, max_age = LIMITS[key]
        derived = DERIVED.get(key, {})
        feeds.provider(key)(StoreFeed(store, key, stale_after, max_age, source=derived.get("source"),
                                      delay=derived.get("delay", 0), daily=derived.get("daily", False)))
        if derived:   # a delayed copy carries the same provenance as its source
            src = feeds._SOURCES.get(derived["source"])
            if src:
                feeds._SOURCES[key] = src
