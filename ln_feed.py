"""ln-network: Lightning Network statistics from AurAgrid's own LND node.

No third-party source: computed from the public channel graph that the local
LND node has learned through gossip (GetNetworkInfo + DescribeGraph), read with
the readonly macaroon. One full graph read (~40 MB, local) per refresh.

Routing policies are counted per direction (one policy per channel side).
"""
import os
import time
from datetime import datetime, timezone
from statistics import median

import requests

import feeds

LND_REST = os.environ.get("LND_REST_URL", "https://127.0.0.1:8081").rstrip("/")
LND_TLS = os.environ.get("LND_TLS_CERT", os.path.expanduser("~/.lnd/tls.cert"))
READONLY_MACAROON = os.environ.get(
    "LND_READONLY_MACAROON", os.path.expanduser("~/.lnd/data/chain/bitcoin/mainnet/readonly.macaroon"))


def _get(path: str, session) -> dict:
    with open(READONLY_MACAROON, "rb") as fh:
        mac = fh.read().hex()
    r = session.get(LND_REST + path, headers={"Grpc-Metadata-macaroon": mac}, verify=LND_TLS, timeout=120)
    r.raise_for_status()
    return r.json()


def percentiles(values: list, ps=(10, 25, 50, 75, 90)) -> dict:
    if not values:
        return {}
    s = sorted(values)
    return {"p%d" % p: s[min(len(s) - 1, int(round(p / 100 * (len(s) - 1))))] for p in ps}


def build_snapshot(info: dict, graph: dict, now=None) -> dict:
    now = now or time.time()
    policies = []
    for edge in graph.get("edges", []):
        for side in ("node1_policy", "node2_policy"):
            pol = edge.get(side)
            if pol:
                policies.append(pol)
    enabled = [p for p in policies if not p.get("disabled")]
    fee_ppm = [int(p.get("fee_rate_milli_msat") or 0) for p in enabled]
    base_msat = [int(p.get("fee_base_msat") or 0) for p in enabled]
    cltv = [int(p.get("time_lock_delta") or 0) for p in enabled]
    fresh = sum(1 for p in policies if now - int(p.get("last_update") or 0) <= 86400)
    capacities = [int(e.get("capacity") or 0) for e in graph.get("edges", [])]
    if not capacities:
        raise RuntimeError("LND returned an empty channel graph")
    return {
        "dataset": "AurAgrid Lightning Network Monitor",
        "observed_at": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "nodes": int(info["num_nodes"]),
        "channels": int(info["num_channels"]),
        "total_capacity_btc": round(int(info["total_network_capacity"]) / 1e8, 2),
        "channel_size_sat": {"avg": round(float(info["avg_channel_size"])), "median": int(info["median_channel_size_sat"]),
                             **percentiles(capacities)},
        "graph_diameter": info.get("graph_diameter"),
        "avg_out_degree": round(float(info.get("avg_out_degree") or 0), 2),
        "routing_policies": {
            "total": len(policies), "enabled": len(enabled),
            "disabled_share_pct": round((len(policies) - len(enabled)) / len(policies) * 100, 1) if policies else None,
            "updated_last_24h": fresh,
            "fee_rate_ppm": percentiles(fee_ppm),
            "base_fee_msat": percentiles(base_msat),
            "time_lock_delta_median": median(cltv) if cltv else None,
        },
        "notes": "Public channel graph as seen by AurAgrid's LND node via gossip. Fee percentiles over "
                 "enabled routing policies (one per channel direction).",
    }


def fetch(session=None) -> dict:
    session = session or requests.Session()
    return build_snapshot(_get("/v1/graph/info", session), _get("/v1/graph", session))


feeds.describe(
    "ln-network", source="AurAgrid's own LND node (Lightning mainnet gossip graph)",
    url="lnd-rest:/v1/graph/info,/v1/graph",
    terms_url="/terms",
    attribution="Computed by AurAgrid from the public Lightning gossip graph seen by its own LND node.",
    as_of=lambda snap: snap["observed_at"])
