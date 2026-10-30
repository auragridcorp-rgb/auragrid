"""Request-time L402 usage ledger.

Aperture's invoice memos don't say which service was bought, and with dynamic
pricing the invoice amount can't identify it either. But every paid request
reaches the backend carrying `Authorization: L402 <macaroon>:<preimage>`, and
sha256(preimage) is the invoice's payment hash. So the backend can record
(payment_hash -> endpoint, call count) as requests are served, and the
dashboard can join that to settled invoices for exact revenue attribution.

Trust model: the hash is computed from the preimage, never read from a header,
so a forged header can't claim a payment it doesn't hold the preimage for.
The dashboard also only uses hashes that match real settled invoices.

Usage (any FastAPI/Starlette app that serves /api/v1/* behind Aperture):

    ledger = Ledger("ledger.db")
    app.add_middleware(L402LedgerMiddleware, ledger=ledger, normalize=normalize_path)

Each row also records the plan the token was bought under (hourly, pass_30d,
bundle), taken from the path the client requested, so revenue can be split by
plan. Free paths (quotes, samples) are skipped entirely.

Check that it works: after a paid call, `ledger.usage_by_endpoint()` should be
non-empty. If `ledger.served_without_token` climbs while the table stays empty,
Aperture is not forwarding the Authorization header to your backend.
"""
import base64
import hashlib
import logging
import queue
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Callable, Optional

log = logging.getLogger("auragrid.ledger")

_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


def hash_hex(value) -> Optional[str]:
    """Normalize an LND r_hash (bytes, 64-char hex, or base64) to lowercase hex."""
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex() if len(value) == 32 else None
    text = str(value).strip()
    if _HEX64.match(text):
        return text.lower()
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_")
    except Exception:
        return None
    return raw.hex() if len(raw) == 32 else None


def token_payment_hash(authorization: str) -> Optional[str]:
    """sha256(preimage) from 'L402 <macaroon>:<preimage>' (or legacy 'LSAT ...')."""
    if not authorization:
        return None
    scheme, _, credential = authorization.strip().partition(" ")
    if scheme.upper() not in ("L402", "LSAT"):
        return None
    preimage = credential.strip().rpartition(":")[2]
    if not _HEX64.match(preimage):
        return None
    return hashlib.sha256(bytes.fromhex(preimage)).hexdigest()


class Ledger:
    def __init__(self, path: str = "ledger.db"):
        self.path = path
        self.served_without_token = 0   # in-memory diagnostic, resets on restart
        self._queue: "queue.Queue" = queue.Queue(maxsize=10_000)
        with self._db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS token_usage (
                       payment_hash TEXT PRIMARY KEY,
                       endpoint     TEXT NOT NULL,
                       first_seen   INTEGER NOT NULL,
                       last_seen    INTEGER NOT NULL,
                       calls        INTEGER NOT NULL DEFAULT 0,
                       plan         TEXT NOT NULL DEFAULT 'hourly'
                   )"""
            )
            columns = {row[1] for row in conn.execute("PRAGMA table_info(token_usage)")}
            if "plan" not in columns:  # ledgers created before plans existed
                conn.execute("ALTER TABLE token_usage ADD COLUMN plan TEXT NOT NULL DEFAULT 'hourly'")
        self._thread = threading.Thread(target=self._writer, name="ledger-writer", daemon=True)
        self._thread.start()

    @contextmanager
    def _db(self):
        conn = sqlite3.connect(self.path, timeout=5)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            with conn:  # commit on success
                yield conn
        finally:
            conn.close()

    # -- write path (never blocks a request) ----------------------------------
    def record(self, payment_hash: str, endpoint: str, plan: str = "hourly") -> None:
        try:
            self._queue.put_nowait((payment_hash, endpoint, plan, int(time.time())))
        except queue.Full:
            log.warning("Ledger queue full; dropping one usage record")

    def _writer(self) -> None:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            while True:
                item = self._queue.get()
                try:
                    if item is None:
                        return
                    payment_hash, endpoint, plan, now = item
                    with conn:
                        conn.execute(
                            """INSERT INTO token_usage (payment_hash, endpoint, first_seen, last_seen, calls, plan)
                               VALUES (?, ?, ?, ?, 1, ?)
                               ON CONFLICT(payment_hash) DO UPDATE SET
                                   calls = calls + 1, last_seen = excluded.last_seen""",
                            (payment_hash, endpoint, now, now, plan),
                        )
                except Exception:
                    log.exception("Ledger write failed")
                finally:
                    self._queue.task_done()
        finally:
            conn.close()

    def flush(self) -> None:
        self._queue.join()

    def stop(self) -> None:
        self._queue.put(None)

    # -- read path ---------------------------------------------------------------
    def endpoints_by_hash(self) -> dict:
        with self._db() as conn:
            return dict(conn.execute("SELECT payment_hash, endpoint FROM token_usage"))

    def plans_by_hash(self) -> dict:
        with self._db() as conn:
            return dict(conn.execute("SELECT payment_hash, plan FROM token_usage"))

    def usage_by_plan(self) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT plan, COUNT(*), COALESCE(SUM(calls), 0) FROM token_usage GROUP BY plan"
            )
            return {plan: {"tokens": tokens, "calls": calls} for plan, tokens, calls in rows}

    def usage_by_endpoint(self) -> dict:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT endpoint, COUNT(*), COALESCE(SUM(calls), 0) FROM token_usage GROUP BY endpoint"
            )
            return {ep: {"tokens": tokens, "calls": calls} for ep, tokens, calls in rows}


class L402LedgerMiddleware:
    """Pure-ASGI middleware: records successful paid requests under /api/v1/.

    Must wrap any path-rewriting middleware (be added after it) so `classify`
    sees the path the client paid for, e.g. /api/v1/pro/ercot-lmp.
    """

    def __init__(self, app, ledger: Ledger, prefix: str = "/api/v1/",
                 skip_prefixes=("/api/v1/price/", "/api/v1/sample/"),
                 normalize: Callable[[str], str] = lambda p: p,
                 classify: Callable[[str], str] = lambda p: "hourly"):
        self.app = app
        self.ledger = ledger
        self.prefix = prefix
        self.skip_prefixes = tuple(skip_prefixes)
        self.normalize = normalize
        self.classify = classify

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "") if scope["type"] == "http" else ""
        if not path.startswith(self.prefix) or path.startswith(self.skip_prefixes):
            await self.app(scope, receive, send)
            return

        status = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            if 200 <= status["code"] < 300:
                self._record(scope, path)

    def _record(self, scope, path: str) -> None:
        try:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode("latin-1")
            payment_hash = token_payment_hash(auth)
            if payment_hash:
                self.ledger.record(payment_hash, self.normalize(path), self.classify(path))
            else:
                self.ledger.served_without_token += 1
        except Exception:
            log.exception("Ledger middleware error")
