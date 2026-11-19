"""Read-only LND REST client for the operator dashboard.

Uses a readonly macaroon and verifies LND's TLS certificate. Every failure
raises: the dashboard shows "LND unavailable" (HTTP 502) rather than numbers
that did not come from the node.

Environment (all optional)
--------------------------
LND_REST_URL            default https://127.0.0.1:8081
LND_TLS_CERT            default ~/.lnd/tls.cert
LND_READONLY_MACAROON   default ~/.lnd/data/chain/bitcoin/mainnet/readonly.macaroon
LND_MAX_INVOICES        newest invoices fetched per refresh (default 5000)
"""
import os

import requests

_HOME = os.path.expanduser("~")


class LNDClient:
    def __init__(self):
        self.base_url = os.environ.get("LND_REST_URL", "https://127.0.0.1:8081").rstrip("/")
        self.tls_cert = os.environ.get("LND_TLS_CERT", os.path.join(_HOME, ".lnd", "tls.cert"))
        self.macaroon_path = os.environ.get(
            "LND_READONLY_MACAROON",
            os.path.join(_HOME, ".lnd", "data", "chain", "bitcoin", "mainnet", "readonly.macaroon"))
        self.max_invoices = int(os.environ.get("LND_MAX_INVOICES", "5000"))
        self.session = requests.Session()

    def _get(self, endpoint: str, params=None) -> dict:
        with open(self.macaroon_path, "rb") as fh:
            macaroon = fh.read().hex()
        resp = self.session.get(self.base_url + endpoint, params=params, timeout=5,
                                headers={"Grpc-Metadata-macaroon": macaroon}, verify=self.tls_cert)
        resp.raise_for_status()
        return resp.json()

    def get_invoices(self) -> list:
        return self.get_invoices_page()[0]

    def get_invoices_page(self):
        """(newest invoices up to max_invoices, total invoices ever created)."""
        data = self._get("/v1/invoices", {"num_max_invoices": self.max_invoices, "reversed": "true"})
        invoices = data.get("invoices") or []
        # last_index_offset of a reversed query = add_index of the newest invoice = all-time count.
        total = int(data.get("last_index_offset") or len(invoices))
        return invoices, max(total, len(invoices))

    def get_channels(self) -> dict:
        return self._get("/v1/channels")

    def get_mainnet_node_stats(self) -> dict:
        info = self._get("/v1/getinfo")
        channels = self.get_channels().get("channels") or []
        chains = info.get("chains") or [{}]
        return {
            "node_alias": info.get("alias"),
            "network": chains[0].get("network"),
            "active_channels": int(info.get("num_active_channels") or 0),
            "total_capacity": sum(int(c.get("capacity") or 0) for c in channels),
            "synced_to_chain": bool(info.get("synced_to_chain")),
        }


lnd = LNDClient()
