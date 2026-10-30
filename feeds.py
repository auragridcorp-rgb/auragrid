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
import json
import logging
import os
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


class FeedWarmingUp(RuntimeError):
    """No snapshot yet: the first background refresh has not finished."""


class BackgroundFeed:
    """For sources too slow to call inside a request (minutes, not seconds).

    A daemon thread calls `fetch` every `interval` seconds (every `retry_seconds`
    after a failure); requests get the last finished snapshot immediately and
    never wait on the source. With `state_path` the last snapshot is saved, so a
    restart serves it at once instead of warming up again. Raises FeedWarmingUp
    until the first snapshot exists, and RuntimeError once it is older than
    `max_stale`. Adds "fetched_at" and "stale" (older than two intervals).
    """

    def __init__(self, fetch, interval: float, max_stale: float, retry_seconds: float = 300,
                 state_path: Optional[str] = None, clock=time.time, name: str = "feed"):
        self.fetch, self.interval, self.max_stale = fetch, interval, max_stale
        self.retry_seconds, self.state_path, self.clock, self.name = retry_seconds, state_path, clock, name
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._value, self._at = None, 0.0
        self.last_error = None
        self._load()

    # -- persistence -----------------------------------------------------------
    def _load(self):
        if not self.state_path:
            return
        try:
            with open(self.state_path) as fh:
                saved = json.load(fh)
            self._value, self._at = saved["value"], float(saved["at"])
        except FileNotFoundError:
            pass
        except Exception:
            log.exception("Ignoring unreadable snapshot file %s", self.state_path)

    def _save(self, value, at):
        if not self.state_path:
            return
        tmp = self.state_path + ".tmp"
        try:
            with open(tmp, "w") as fh:
                json.dump({"at": at, "value": value}, fh)
            os.replace(tmp, self.state_path)
        except Exception:
            log.exception("Could not save snapshot for %s", self.name)

    # -- refreshing --------------------------------------------------------------
    def current(self):
        with self._lock:
            return self._value

    def refresh_once(self) -> bool:
        try:
            value = self.fetch()
        except Exception as exc:
            self.last_error = "%s: %s" % (type(exc).__name__, exc)
            log.exception("Background refresh of %s failed; retrying in %ds", self.name, self.retry_seconds)
            return False
        at = self.clock()
        with self._lock:
            self._value, self._at = value, at
        self.last_error = None
        self._save(value, at)
        return True

    def start(self):
        threading.Thread(target=self._loop, name="refresh-" + self.name, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            ok = self.refresh_once()
            if self._stop.wait(self.interval if ok else self.retry_seconds):
                return

    # -- serving -----------------------------------------------------------------
    def __call__(self) -> dict:
        with self._lock:
            value, at = self._value, self._at
        if value is None:
            raise FeedWarmingUp("%s: first snapshot still being built" % self.name)
        age = self.clock() - at
        if age > self.max_stale:
            raise RuntimeError("%s data older than %ds" % (self.name, self.max_stale))
        return {**value, "fetched_at": datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds"),
                "stale": age > 2 * self.interval}


# ---------------------------------------------------------------------------
# Provenance: every JSON response names its source (set by each provider module)
# ---------------------------------------------------------------------------
_SOURCES: dict = {}


def describe(key: str, *, source: str, url: str, terms_url: str, attribution: str,
             as_of: Callable[[dict], str]):
    """Register where a feed's data comes from and how to read its own timestamp."""
    _SOURCES[key] = {"source": source, "url": url, "terms_url": terms_url,
                     "attribution": attribution, "as_of": as_of}


def provenance(key: str, snapshot: dict, retrieved_at: str) -> Optional[dict]:
    meta = _SOURCES.get(key)
    if meta is None:
        return None
    try:
        as_of = meta["as_of"](snapshot)
    except Exception:
        as_of = None
    out = {k: meta[k] for k in ("source", "url", "terms_url", "attribution")}
    out.update(retrieved_at=retrieved_at, as_of=as_of)
    return out


def as_of_for(key: str, snapshot: dict) -> Optional[str]:
    meta = _SOURCES.get(key)
    try:
        return meta["as_of"](snapshot) if meta else None
    except Exception:
        return None
