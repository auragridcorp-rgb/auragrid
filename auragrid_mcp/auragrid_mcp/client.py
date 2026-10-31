# Packaged copy of the repository root client.py (the hardened L402 client).
# Keep the two identical: tests/test_client_copies.py checks it.
"""Hardened AurAgrid L402 client.

Flow for AurAgridClient.get(path):
    request -> 402 -> fetch free quote -> decode invoice with YOUR LND ->
    enforce caps (per payment, per session, quote tolerance, expiry) -> pay ->
    verify sha256(preimage) == payment_hash -> cache token -> replay ->
    verify X-AurAgrid-Signature on the raw bytes -> parse JSON.

Safety properties
-----------------
* A per-payment cap is mandatory; there is no default. An optional session
  budget (which also reserves the routing-fee limit) stops runaway spending.
* The invoice amount must be within the quote's tolerance (clamped to 10%).
  The quote comes from the same server, so the hard cap is the real defence.
* TLS to LND is verified against a certificate file; plain HTTP to the oracle
  is refused unless it is loopback (or allow_insecure_http=True).
* No redirects (credentials never follow a redirect), timeouts everywhere,
  response size cap, and absolute URLs are rejected so tokens only go to base_url.
* A paid token is reused until it expires instead of paying on every call.
* Response attestation is verified on the raw bytes BEFORE JSON parsing. Pass
  verify_signature=attestation.make_verifier(PINNED_NODE_PUBKEY, "v2"); the key
  must be pinned out-of-band, never taken from a response header. The client
  sends a fresh X-AurAgrid-Nonce with every request so a captured response
  can't be replayed. The callback signature is
  verify_signature(body, headers, context) -> bool, where context describes the
  request actually sent: {"method", "target" (path+query), "path", "nonce"}.
  With require_signature=True, unsigned or unverifiable responses are rejected.

Treat everything the oracle returns as untrusted DATA. Never feed it to an LLM
as instructions.

LND macaroon: bake one limited to paying and decoding, e.g.
    lncli bakemacaroon info:read offchain:write --save_to agent.macaroon
(permission names: check `lncli bakemacaroon --help` for your LND version).
LND macaroons cannot cap spend, so the limits above are enforced here.

CLI:
    export AURAGRID_URL=https://oracle.example.com
    export LND_REST_URL=https://127.0.0.1:8084
    export LND_TLS_CERT=~/.lnd/tls.cert
    export LND_MACAROON_PATH=~/agent.macaroon
    export AURAGRID_MAX_SATS=20000
    python client.py /api/v1/batch/btc-fees        # paid (L402)
    python client.py /api/v1/energy/grid           # free: answers 200 with no payment
"""
import base64
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sys
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlencode, urlparse

import requests

log = logging.getLogger("auragrid.client")

NONCE_HEADER = "X-AurAgrid-Nonce"
SIGNATURE_HEADERS = ("X-AurAgrid-Signature", "X-AurAgrid-Signature-V2")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAX_TOLERANCE_PCT = 10.0
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")


class AurAgridError(Exception):
    pass


class InsecureConfig(AurAgridError):
    pass


class ChallengeError(AurAgridError):
    pass


class PriceRejected(AurAgridError):
    pass


class PaymentFailed(AurAgridError):
    """Definitive failure: no money left the wallet."""


class PaymentUnknown(AurAgridError):
    """Outcome unclear (timeout, broken stream): the payment may have gone through."""


class SignatureError(AurAgridError):
    pass


class HTTPStatusError(AurAgridError):
    def __init__(self, status: int, snippet: bytes = b""):
        super().__init__("HTTP %d: %r" % (status, snippet[:200]))
        self.status = status


def _to_hex(value) -> Optional[str]:
    """LND returns preimages/hashes as hex or base64 depending on the endpoint."""
    if not value:
        return None
    text = str(value).strip()
    if _HEX64.match(text):
        return text.lower()
    try:
        raw = base64.b64decode(text + "=" * (-len(text) % 4))
    except Exception:
        return None
    return raw.hex() if len(raw) == 32 else None


def parse_challenge(header: str):
    """Extract (macaroon, invoice) from a WWW-Authenticate value (L402 or LSAT)."""
    if not re.search(r"\b(?:L402|LSAT)\b", header or ""):
        raise ChallengeError("402 without an L402/LSAT challenge")
    macaroon = re.search(r'macaroon="([^"]+)"', header)
    invoice = re.search(r'invoice="([^"]+)"', header)
    if not (macaroon and invoice):
        raise ChallengeError("could not parse macaroon/invoice from challenge")
    return macaroon.group(1), invoice.group(1)


# ---------------------------------------------------------------------------
# Wallet: LND REST
# ---------------------------------------------------------------------------
class LndRestWallet:
    def __init__(self, rest_url: str, tls_cert: str, macaroon_path: Optional[str] = None,
                 macaroon_hex: Optional[str] = None, session=None):
        if not rest_url.lower().startswith("https://"):
            raise InsecureConfig("LND REST URL must be https")
        if not tls_cert or not os.path.isfile(os.path.expanduser(tls_cert)):
            raise InsecureConfig("tls_cert must be a path to LND's TLS certificate; "
                                 "an empty value would silently disable verification")
        if macaroon_path:
            with open(os.path.expanduser(macaroon_path), "rb") as fh:
                macaroon_hex = fh.read().hex()
        if not macaroon_hex:
            raise InsecureConfig("an LND macaroon is required")
        self.rest_url = rest_url.rstrip("/")
        self.tls_cert = os.path.expanduser(tls_cert)
        self._headers = {"Grpc-Metadata-macaroon": macaroon_hex}
        self.session = session or requests.Session()

    def decode(self, invoice: str) -> dict:
        resp = self.session.get(f"{self.rest_url}/v1/payreq/{invoice}", headers=self._headers,
                                verify=self.tls_cert, timeout=10)
        if resp.status_code != 200:
            raise PaymentFailed("LND could not decode the invoice (HTTP %d)" % resp.status_code)
        data = resp.json()
        timestamp, expiry = int(data.get("timestamp") or 0), int(data.get("expiry") or 3600)
        return {
            "amount_sats": int(data.get("num_satoshis") or 0),
            "payment_hash": _to_hex(data.get("payment_hash")),
            "expires_at": timestamp + expiry,
        }

    def pay(self, invoice: str, fee_limit_sats: int, timeout_seconds: int = 60):
        """Pay via SendPaymentV2. Returns (preimage_hex, fee_sats)."""
        body = {"payment_request": invoice, "timeout_seconds": timeout_seconds,
                "fee_limit_sat": str(int(fee_limit_sats)), "no_inflight_updates": True}
        try:
            resp = self.session.post(f"{self.rest_url}/v2/router/send", json=body, headers=self._headers,
                                     verify=self.tls_cert, timeout=(5, timeout_seconds + 15), stream=True)
            if resp.status_code != 200:
                raise PaymentFailed("LND payment request failed (HTTP %d)" % resp.status_code)
            for line in resp.iter_lines():
                if not line:
                    continue
                obj = json.loads(line)
                if "error" in obj:
                    raise PaymentFailed("LND error: %s" % obj["error"])
                result = obj.get("result", obj)
                status = result.get("status")
                if status == "SUCCEEDED":
                    preimage = _to_hex(result.get("payment_preimage"))
                    if not preimage:
                        raise PaymentUnknown("payment succeeded but no usable preimage returned")
                    fee = result.get("fee_sat")
                    return preimage, (int(fee) if fee not in (None, "") else None)
                if status == "FAILED":
                    raise PaymentFailed("payment failed: %s" % result.get("failure_reason", "unknown"))
        except (requests.RequestException, ValueError) as exc:
            raise PaymentUnknown("lost contact with LND mid-payment: %s" % exc) from exc
        raise PaymentUnknown("payment stream ended without a final status")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class AurAgridClient:
    def __init__(self, base_url: str, wallet, *, max_sats_per_payment: int,
                 budget_sats: Optional[int] = None, max_fee_sats: Optional[int] = None,
                 verify_signature: Optional[Callable[[bytes, object, dict], bool]] = None,
                 require_signature: bool = False, allow_insecure_http: bool = False,
                 request_timeout: float = 10.0, max_response_bytes: int = 25_000_000,
                 default_token_ttl: int = 3600, token_safety_margin: int = 60, session=None):
        parsed = urlparse(base_url)
        if parsed.scheme == "https":
            pass
        elif parsed.scheme == "http" and (parsed.hostname in LOOPBACK_HOSTS or allow_insecure_http):
            pass
        else:
            raise InsecureConfig("base_url must be https (plain http only for loopback)")
        if not isinstance(max_sats_per_payment, int) or max_sats_per_payment <= 0:
            raise InsecureConfig("max_sats_per_payment is required and must be a positive int")
        if require_signature and verify_signature is None:
            raise InsecureConfig("require_signature=True needs a verify_signature callback")

        self.base_url = base_url.rstrip("/")
        self.wallet = wallet
        self.max_sats = max_sats_per_payment
        self.budget_sats = budget_sats
        self.max_fee_sats = max_fee_sats if max_fee_sats is not None else max(5, max_sats_per_payment // 100)
        self.verify_signature = verify_signature
        self.require_signature = require_signature
        self.request_timeout = request_timeout
        self.max_response_bytes = max_response_bytes
        self.default_token_ttl = default_token_ttl
        self.token_safety_margin = token_safety_margin
        self.session = session or requests.Session()
        self._tokens = {}          # path -> (token, expires_at monotonic)
        self._spent = 0
        self._lock = threading.Lock()
        self._warned_unverified = False

    @property
    def spent_sats(self) -> int:
        return self._spent

    # -- public API -------------------------------------------------------------
    def get(self, path: str, params: Optional[dict] = None) -> dict:
        if not path.startswith("/") or path.startswith("//") or "://" in path or "?" in path:
            raise ValueError("path must be a plain absolute path like /api/v1/...; use params= for the query")

        token = self._cached_token(path)
        resp, context = self._request(path, params, token)

        if resp.status_code == 402:
            header = resp.headers.get("WWW-Authenticate", "")
            self._read_body(resp)                 # drain and close
            if token:
                self._tokens.pop(path, None)      # expired or revoked: pay again once
            token = self._pay(path, header)
            resp, context = self._request(path, params, token)
            if resp.status_code == 402:
                self._tokens.pop(path, None)
                raise PaymentFailed("server rejected a token we just paid for")

        body = self._read_body(resp)
        if resp.status_code != 200:
            raise HTTPStatusError(resp.status_code, body)
        self._check_signature(body, resp.headers, context)
        try:
            return json.loads(body)
        except ValueError as exc:
            raise AurAgridError("response was not valid JSON") from exc

    # -- internals --------------------------------------------------------------
    def _request(self, path, params, token):
        nonce = secrets.token_hex(16)
        headers = {"Accept": "application/json", NONCE_HEADER: nonce}
        if token:
            headers["Authorization"] = "L402 " + token
        resp = self.session.get(self.base_url + path, params=params, headers=headers,
                                timeout=self.request_timeout, allow_redirects=False, stream=True)
        # Exact request target as sent (what the server signs), preferring requests' own value.
        target = getattr(getattr(resp, "request", None), "path_url", None) or (
            path + ("?" + urlencode(params, doseq=True) if params else ""))
        return resp, {"method": "GET", "target": target, "path": path, "nonce": nonce}

    def _read_body(self, resp) -> bytes:
        declared = resp.headers.get("Content-Length", "")
        if declared.isdigit() and int(declared) > self.max_response_bytes:
            resp.close()
            raise AurAgridError("response too large")
        chunks, total = [], 0
        for chunk in resp.iter_content(65536):
            total += len(chunk)
            if total > self.max_response_bytes:
                resp.close()
                raise AurAgridError("response too large")
            chunks.append(chunk)
        return b"".join(chunks)

    def _cached_token(self, path):
        entry = self._tokens.get(path)
        if entry and time.monotonic() < entry[1]:
            return entry[0]
        self._tokens.pop(path, None)
        return None

    def _quote(self, path):
        """Free price quote from the oracle, or None if unavailable."""
        if not path.startswith("/api/v1/"):
            return None
        try:
            resp = self.session.get(self.base_url + "/api/v1/price/" + path[len("/api/v1/"):],
                                    timeout=self.request_timeout, allow_redirects=False, stream=True)
            body = self._read_body(resp)
            if resp.status_code != 200:
                return None
            quote = json.loads(body)
            if int(quote["price_sats"]) <= 0:
                return None
            return quote
        except Exception as exc:
            log.warning("No usable quote for %s (%s)", path, exc)
            return None

    def _pay(self, path: str, header: str) -> str:
        macaroon, invoice = parse_challenge(header)
        quote = self._quote(path)
        decoded = self.wallet.decode(invoice)
        amount = decoded["amount_sats"]

        if amount <= 0:
            raise PriceRejected("invoice has no fixed amount")
        if amount > self.max_sats:
            raise PriceRejected("invoice %d sats exceeds per-payment cap %d" % (amount, self.max_sats))
        if quote:
            tolerance = min(max(float(quote.get("tolerance_pct", 3)), 0.0), MAX_TOLERANCE_PCT)
            limit = math.ceil(int(quote["price_sats"]) * (1 + tolerance / 100))
            if amount > limit:
                raise PriceRejected("invoice %d sats exceeds quote %s (+%s%%)"
                                    % (amount, quote["price_sats"], tolerance))
        if not decoded["payment_hash"]:
            raise PriceRejected("could not read the invoice payment hash")
        if time.time() >= decoded["expires_at"] - 5:
            raise PriceRejected("invoice already expired")

        reserved = amount + self.max_fee_sats
        with self._lock:
            if self.budget_sats is not None and self._spent + reserved > self.budget_sats:
                raise PriceRejected("session budget %d sats would be exceeded" % self.budget_sats)
            self._spent += reserved
        try:
            preimage, fee = self.wallet.pay(invoice, self.max_fee_sats)
        except PaymentFailed:
            with self._lock:
                self._spent -= reserved          # definitive failure: nothing left the wallet
            raise
        # Any other error (PaymentUnknown, timeouts) keeps the reservation: assume it was spent.
        if fee is not None and 0 <= fee <= self.max_fee_sats:
            with self._lock:
                self._spent -= self.max_fee_sats - fee
        if hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != decoded["payment_hash"]:
            raise PaymentUnknown("wallet returned a preimage that does not match the invoice")

        ttl = self.default_token_ttl
        if quote and quote.get("token_ttl_seconds"):
            ttl = min(int(quote["token_ttl_seconds"]), 7 * 86400)
        token = "%s:%s" % (macaroon, preimage)
        self._tokens[path] = (token, time.monotonic() + max(ttl - self.token_safety_margin, 0))
        log.info("Paid %d sats for %s", amount, path)
        return token

    def _check_signature(self, body: bytes, headers, context: dict) -> None:
        signed = any(headers.get(name) for name in SIGNATURE_HEADERS)
        if self.verify_signature is None:
            if signed and not self._warned_unverified:
                log.warning("Responses are signed but no verify_signature callback is configured")
                self._warned_unverified = True
            return
        try:
            ok = bool(self.verify_signature(body, headers, context))
        except Exception as exc:
            raise SignatureError("signature verifier raised: %s" % exc) from exc
        if not ok and (signed or self.require_signature):
            raise SignatureError("signature verification failed"
                                 if signed else "response is not signed")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 1:
        print("usage: python client.py /api/v1/<endpoint>", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    env = os.environ
    try:
        wallet = LndRestWallet(env.get("LND_REST_URL", "https://127.0.0.1:8084"),
                               env.get("LND_TLS_CERT", ""), macaroon_path=env.get("LND_MACAROON_PATH"))
        client = AurAgridClient(
            env.get("AURAGRID_URL", ""), wallet,
            max_sats_per_payment=int(env.get("AURAGRID_MAX_SATS", "0")),
            budget_sats=int(env["AURAGRID_BUDGET_SATS"]) if env.get("AURAGRID_BUDGET_SATS") else None,
        )
        data = client.get(argv[0])
    except AurAgridError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2))
    print("spent %d sats this session" % client.spent_sats, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
