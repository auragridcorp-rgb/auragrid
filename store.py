"""Snapshot store for feed data: SQLite (WAL) with rolling retention.

  raw    every new snapshot per feed, kept RAW_DAYS (default 7) days
  daily  the last snapshot of each UTC day per feed, kept forever

A snapshot is only written after a successful fetch, so a failure can never
overwrite good data. A snapshot whose `as_of` equals the newest stored one is
not stored again (sources like EIA monthly data rarely change). Payloads are
zlib-compressed JSON. The writer is the fetcher process; the API only reads.

Environment
-----------
AURAGRID_FEEDS_DB        database path (default var/feeds.db next to this file)
AURAGRID_RAW_DAYS        raw retention in days (default 7)
AURAGRID_DB_BUDGET_MB    warn when the database grows past this (default 2048)
"""
import json
import logging
import os
import sqlite3
import time
import zlib
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("auragrid.store")

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("AURAGRID_FEEDS_DB", os.path.join(HERE, "var", "feeds.db"))
RAW_DAYS = int(os.environ.get("AURAGRID_RAW_DAYS", "7"))
BUDGET_BYTES = int(os.environ.get("AURAGRID_DB_BUDGET_MB", "2048")) * 1024 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw (
    feed TEXT NOT NULL, retrieved_at REAL NOT NULL, as_of TEXT NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (feed, retrieved_at));
CREATE TABLE IF NOT EXISTS daily (
    feed TEXT NOT NULL, day TEXT NOT NULL, retrieved_at REAL NOT NULL, as_of TEXT NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (feed, day));
CREATE TABLE IF NOT EXISTS fetch_status (
    feed TEXT PRIMARY KEY, last_attempt REAL, last_success REAL, last_error TEXT, consecutive_failures INTEGER);
"""


def _pack(value) -> bytes:
    return zlib.compress(json.dumps(value, separators=(",", ":")).encode(), 6)


def _unpack(blob: bytes):
    return json.loads(zlib.decompress(blob))


class Snapshot:
    __slots__ = ("feed", "data", "as_of", "retrieved_at")

    def __init__(self, feed, data, as_of, retrieved_at):
        self.feed, self.data, self.as_of, self.retrieved_at = feed, data, as_of, retrieved_at

    @property
    def retrieved_iso(self) -> str:
        return datetime.fromtimestamp(self.retrieved_at, timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = DB_PATH, raw_days: int = RAW_DAYS, clock=time.time):
        self.path, self.raw_days, self.clock = path, raw_days, clock
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._db() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            with conn:
                yield conn
        finally:
            conn.close()

    # -- writes (fetcher only) ---------------------------------------------------
    def save(self, feed: str, data, as_of: str) -> bool:
        """Store a successfully fetched snapshot. Returns False if as_of is unchanged."""
        now = self.clock()
        day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
        blob = _pack(data)
        with self._db() as conn:
            row = conn.execute("SELECT as_of FROM raw WHERE feed=? ORDER BY retrieved_at DESC LIMIT 1",
                               (feed,)).fetchone()
            self._status(conn, feed, now, ok=True)
            if row and row[0] == as_of:
                return False
            conn.execute("INSERT OR REPLACE INTO raw VALUES (?,?,?,?)", (feed, now, as_of, blob))
            conn.execute("INSERT INTO daily VALUES (?,?,?,?,?) ON CONFLICT(feed, day) DO UPDATE SET "
                         "retrieved_at=excluded.retrieved_at, as_of=excluded.as_of, payload=excluded.payload",
                         (feed, day, now, as_of, blob))
        return True

    def record_failure(self, feed: str, error: str) -> None:
        with self._db() as conn:
            self._status(conn, feed, self.clock(), ok=False, error=error[:500])

    @staticmethod
    def _status(conn, feed, now, ok, error=None):
        conn.execute(
            "INSERT INTO fetch_status VALUES (?,?,?,?,?) ON CONFLICT(feed) DO UPDATE SET "
            "last_attempt=excluded.last_attempt, "
            "last_success=COALESCE(excluded.last_success, fetch_status.last_success), "
            "last_error=excluded.last_error, "
            "consecutive_failures=CASE WHEN excluded.last_error IS NULL THEN 0 "
            "ELSE fetch_status.consecutive_failures + 1 END",
            (feed, now, now if ok else None, None if ok else error, 0 if ok else 1))

    # -- reads (API) -------------------------------------------------------------------
    def latest(self, feed: str) -> Optional[Snapshot]:
        with self._db() as conn:
            row = conn.execute("SELECT payload, as_of, retrieved_at FROM raw WHERE feed=? "
                               "ORDER BY retrieved_at DESC LIMIT 1", (feed,)).fetchone() or \
                conn.execute("SELECT payload, as_of, retrieved_at FROM daily WHERE feed=? "
                             "ORDER BY day DESC LIMIT 1", (feed,)).fetchone()
        return Snapshot(feed, _unpack(row[0]), row[1], row[2]) if row else None

    def latest_before(self, feed: str, cutoff: float) -> Optional[Snapshot]:
        """Newest snapshot retrieved at or before `cutoff` (for delayed free copies)."""
        with self._db() as conn:
            row = conn.execute("SELECT payload, as_of, retrieved_at FROM raw WHERE feed=? AND retrieved_at<=? "
                               "ORDER BY retrieved_at DESC LIMIT 1", (feed, cutoff)).fetchone()
        return Snapshot(feed, _unpack(row[0]), row[1], row[2]) if row else None

    def status(self, feed: str) -> Optional[dict]:
        with self._db() as conn:
            row = conn.execute("SELECT last_attempt, last_success, last_error, consecutive_failures "
                               "FROM fetch_status WHERE feed=?", (feed,)).fetchone()
        return dict(zip(("last_attempt", "last_success", "last_error", "consecutive_failures"), row)) if row else None

    def daily_history(self, feed: str, limit: int = 90) -> list:
        with self._db() as conn:
            rows = conn.execute("SELECT day, as_of, payload, retrieved_at FROM daily WHERE feed=? "
                                "ORDER BY day DESC LIMIT ?", (feed, limit)).fetchall()
        return [{"day": d, "as_of": a, "data": _unpack(p), "retrieved_at": r} for d, a, p, r in rows]

    # -- housekeeping ----------------------------------------------------------------
    def prune(self) -> int:
        cutoff = self.clock() - self.raw_days * 86400
        with self._db() as conn:
            return conn.execute("DELETE FROM raw WHERE retrieved_at < ?", (cutoff,)).rowcount

    def vacuum(self) -> None:
        conn = sqlite3.connect(self.path, timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("VACUUM")
        finally:
            conn.close()

    def size_bytes(self) -> int:
        return sum(os.path.getsize(p) for p in (self.path, self.path + "-wal") if os.path.exists(p))

    def over_budget(self) -> bool:
        size = self.size_bytes()
        if size > BUDGET_BYTES:
            log.error("Feed database is %.0f MB, over the %d MB budget", size / 2**20, BUDGET_BYTES // 2**20)
            return True
        return False
