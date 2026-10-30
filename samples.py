"""Free, delayed, truncated samples of the hero feeds (no paywall).

A background thread snapshots each sample feed from its real provider (see
feeds.py) every AURAGRID_SAMPLE_INTERVAL seconds. GET /api/v1/sample/<feed>
serves the newest snapshot that is at least AURAGRID_SAMPLE_DELAY seconds old,
with every list cut to AURAGRID_SAMPLE_MAX_ITEMS entries. So a sample is real
data, but always stale and partial, and never a substitute for the paid feed.

Until the first snapshot has aged past the delay, the route returns 503
(warming up). Snapshots are persisted to AURAGRID_SAMPLES_DIR so a restart
does not restart the delay window.

Environment (all optional)
--------------------------
AURAGRID_SAMPLE_DELAY       seconds a snapshot must age before serving (default 3600)
AURAGRID_SAMPLE_INTERVAL    seconds between snapshots                  (default 900)
AURAGRID_SAMPLE_MAX_ITEMS   max entries kept per list                   (default 10)
AURAGRID_SAMPLES_DIR        snapshot directory                          (default samples)
"""
import json
import logging
import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Optional

import feeds

log = logging.getLogger("auragrid.samples")

DELAY_SECONDS = int(os.environ.get("AURAGRID_SAMPLE_DELAY", "3600"))
INTERVAL_SECONDS = int(os.environ.get("AURAGRID_SAMPLE_INTERVAL", "900"))
MAX_ITEMS = int(os.environ.get("AURAGRID_SAMPLE_MAX_ITEMS", "10"))
SAMPLES_DIR = os.environ.get("AURAGRID_SAMPLES_DIR", "samples")

# feed key -> accepted URL slugs (canonical first)
SAMPLE_FEEDS = {
    "ercot-lmp": ("ercot-lmp",),
    "lng-supply-chain": ("lng-supply-chain", "lng-supply"),
    "port-congestion-index": ("port-congestion-index", "port-congestion"),
}
_ALIASES = {alias: key for key, aliases in SAMPLE_FEEDS.items() for alias in aliases}


def resolve(slug: str) -> Optional[str]:
    return _ALIASES.get(slug)


def truncate(data, limit: int = MAX_ITEMS, path: str = "$", cut: Optional[dict] = None):
    """Cut every list in `data` to `limit` items. Returns (data, {json_path: original_len})."""
    cut = {} if cut is None else cut
    if isinstance(data, list):
        if len(data) > limit:
            cut[path] = len(data)
        return [truncate(v, limit, "%s[%d]" % (path, i), cut)[0] for i, v in enumerate(data[:limit])], cut
    if isinstance(data, dict):
        return {k: truncate(v, limit, "%s.%s" % (path, k), cut)[0] for k, v in data.items()}, cut
    return data, cut


class SampleStore:
    def __init__(self, directory: str = SAMPLES_DIR, delay: int = DELAY_SECONDS,
                 interval: int = INTERVAL_SECONDS, max_items: int = MAX_ITEMS,
                 fetch: Callable = feeds.fetch, clock: Callable[[], float] = time.time):
        self.directory, self.delay, self.interval = directory, delay, interval
        self.max_items, self.fetch, self.clock = max_items, fetch, clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._snaps = {key: [] for key in SAMPLE_FEEDS}   # key -> [(taken_at, data, truncated)], oldest first
        self._unconnected_logged = set()
        os.makedirs(directory, exist_ok=True)
        for key in SAMPLE_FEEDS:
            self._snaps[key] = self._load(key)

    # -- persistence ---------------------------------------------------------
    def _file(self, key: str) -> str:
        return os.path.join(self.directory, key + ".json")

    def _load(self, key: str) -> list:
        try:
            with open(self._file(key)) as fh:
                return [(float(t), d, c) for t, d, c in json.load(fh)]
        except FileNotFoundError:
            return []
        except Exception:
            log.exception("Ignoring unreadable sample file for %s", key)
            return []

    def _save(self, key: str, snaps: list) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=key, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(snaps, fh)
            os.replace(tmp, self._file(key))
        except Exception:
            log.exception("Could not persist sample snapshot for %s", key)
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # -- snapshotting ---------------------------------------------------------
    def snapshot_once(self) -> None:
        for key in SAMPLE_FEEDS:
            try:
                data = self.fetch(key)
            except feeds.FeedNotConnected:
                if key not in self._unconnected_logged:
                    log.warning("Sample feed %s has no provider registered in feeds.py; it will stay 503", key)
                    self._unconnected_logged.add(key)
                continue
            except Exception:
                log.exception("Snapshot of %s failed", key)
                continue
            now = self.clock()
            sample, cut = truncate(data, self.max_items)   # never keep more than a sample on disk
            keep_after = now - self.delay - 2 * self.interval
            with self._lock:
                snaps = [s for s in self._snaps[key] if s[0] >= keep_after] + [(now, sample, cut)]
                self._snaps[key] = snaps
            self._save(key, snaps)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sample-snapshotter", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while True:
            try:
                self.snapshot_once()
            except Exception:
                log.exception("Sample snapshot loop error")
            if self._stop.wait(self.interval):
                return

    # -- serving ----------------------------------------------------------------
    def get(self, key: str) -> Optional[dict]:
        """The sample body for `key`, or None while warming up."""
        cutoff = self.clock() - self.delay
        with self._lock:
            ready = [s for s in self._snaps.get(key, []) if s[0] <= cutoff]
        if not ready:
            return None
        taken_at, body, cut = ready[-1]
        return {
            "feed": key,
            "mode": "sample_delayed",
            "as_of": datetime.fromtimestamp(taken_at, timezone.utc).isoformat(timespec="seconds"),
            "delay_seconds": self.delay,
            "max_items_per_list": self.max_items,
            "truncated": cut,
            "data": body,
        }
