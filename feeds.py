"""Feed data providers: where the paid /api/v1/* routes get their data.

Register one provider per feed key (the pricer.normalize_path form: "ercot-lmp",
"esg/carbon", ...). A provider is a zero-argument callable that returns
JSON-serializable data and may raise on failure:

    import feeds

    @feeds.provider("ercot-lmp")
    def ercot_lmp():
        return {"nodes": [...], "interval_end": "..."}

Routes for feeds with no provider answer 503 "feed not connected", and the
sample snapshotter skips them. Nothing is ever synthesized here: a paying
customer either gets the real feed or an error.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

log = logging.getLogger("auragrid.feeds")

_PROVIDERS: dict = {}


class FeedNotConnected(LookupError):
    pass


def provider(key: str):
    def register(fn: Callable[[], object]):
        _PROVIDERS[key] = fn
        return fn
    return register


def get(key: str) -> Optional[Callable[[], object]]:
    return _PROVIDERS.get(key)


def connected() -> list:
    return sorted(_PROVIDERS)


def fetch(key: str):
    fn = _PROVIDERS.get(key)
    if fn is None:
        raise FeedNotConnected(key)
    return fn()


class CachedFeed:
    """Wrap a slow fetch: reuse one snapshot for cache_seconds, serve the last good one
    (flagged "stale") while the source is failing, and error once it is older than
    max_stale_seconds. After a failure the source is retried every retry_seconds
    (default: the smaller of cache_seconds and 5 minutes), not on every request.
    Adds "fetched_at" and "stale" to the snapshot dict."""

    def __init__(self, fetch, cache_seconds: float, max_stale_seconds: float,
                 retry_seconds: Optional[float] = None, clock=time.time):
        self.fetch, self.cache_seconds, self.max_stale = fetch, cache_seconds, max_stale_seconds
        self.retry_seconds = retry_seconds if retry_seconds is not None else min(cache_seconds, 300)
        self.clock = clock
        self._lock = threading.Lock()
        self._value, self._at, self._next_try = None, 0.0, 0.0

    def __call__(self) -> dict:
        with self._lock:   # one source call at a time; concurrent requests wait for it
            now = self.clock()
            due = self._value is None or now - self._at >= self.cache_seconds
            if due and now >= self._next_try:
                try:
                    self._value, self._at = self.fetch(), now
                except Exception:
                    self._next_try = now + self.retry_seconds   # don't retry the source on every request
                    if self._value is None:
                        raise
                    log.exception("Feed refresh failed; serving the previous snapshot")
            if self._value is None:
                raise RuntimeError("Feed unavailable; retrying in %ds" % (self._next_try - now))
            if now - self._at > self.max_stale:
                raise RuntimeError("Feed data older than %ds" % self.max_stale)
            age = now - self._at
            return {**self._value,
                    "fetched_at": datetime.fromtimestamp(self._at, timezone.utc).isoformat(timespec="seconds"),
                    "stale": age >= self.cache_seconds}
