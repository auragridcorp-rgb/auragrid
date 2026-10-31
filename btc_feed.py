"""btc-fees: Bitcoin fee estimates and mempool state from AurAgrid's own Bitcoin Core node.

No third-party source: everything is computed by the local pruned mainnet node
(estimatesmartfee, getmempoolinfo, getblockchaininfo, getblockstats). RPC
credentials are read from ~/.bitcoin/bitcoin.conf at call time.

Fee rates are converted from BTC/kvB to sat/vB.
"""
import os
from datetime import datetime, timezone

import requests

import feeds

BITCOIN_CONF = os.environ.get("AURAGRID_BITCOIN_CONF", os.path.expanduser("~/.bitcoin/bitcoin.conf"))
RPC_URL = os.environ.get("AURAGRID_BITCOIN_RPC", "http://127.0.0.1:8332")
TARGETS = (1, 2, 3, 6, 12, 24, 144)


def _conf() -> dict:
    out = {}
    with open(BITCOIN_CONF) as fh:
        for line in fh:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


class BitcoinRPC:
    def __init__(self, session=None):
        self.session = session or requests.Session()

    def __call__(self, method: str, *params):
        conf = _conf()
        r = self.session.post(RPC_URL, auth=(conf["rpcuser"], conf["rpcpassword"]), timeout=30,
                              json={"jsonrpc": "1.0", "id": "auragrid", "method": method, "params": list(params)})
        body = r.json()
        if body.get("error"):
            raise RuntimeError("bitcoind %s: %s" % (method, body["error"]))
        return body["result"]


def sat_per_vb(btc_per_kvb):
    return None if btc_per_kvb is None else round(btc_per_kvb * 1e8 / 1000, 2)


def build_snapshot(rpc) -> dict:
    chain = rpc("getblockchaininfo")
    if chain.get("initialblockdownload") or chain["blocks"] < chain["headers"] - 2:
        raise RuntimeError("Bitcoin node is not synced (%s/%s)" % (chain["blocks"], chain["headers"]))
    estimates = {}
    for target in TARGETS:
        est = rpc("estimatesmartfee", target, "economical")
        estimates[str(target)] = {"sat_per_vb": sat_per_vb(est.get("feerate")), "blocks": est.get("blocks")}
    mempool = rpc("getmempoolinfo")
    stats = rpc("getblockstats", chain["blocks"],
                ["height", "time", "txs", "feerate_percentiles", "avgfeerate", "minfeerate", "maxfeerate",
                 "total_weight", "totalfee"])
    return {
        "dataset": "AurAgrid Bitcoin Fee and Mempool Monitor",
        "network": chain["chain"],
        "tip": {"height": stats["height"], "block_time": stats["time"],
                "block_time_utc": datetime.fromtimestamp(stats["time"], timezone.utc).isoformat(timespec="seconds")},
        "fee_estimates": estimates,
        "mempool": {
            "transactions": mempool["size"], "vsize_bytes": mempool["bytes"],
            "memory_usage_bytes": mempool.get("usage"),
            "min_fee_sat_per_vb": sat_per_vb(mempool.get("mempoolminfee")),
            "total_fee_btc": mempool.get("total_fee"),
        },
        "last_block": {
            "height": stats["height"], "time": stats["time"], "transactions": stats["txs"],
            "weight": stats["total_weight"], "total_fee_sat": stats["totalfee"],
            "avg_fee_rate_sat_per_vb": stats["avgfeerate"],
            "fee_rate_percentiles_sat_per_vb": dict(zip(("p10", "p25", "p50", "p75", "p90"),
                                                         stats["feerate_percentiles"])),
        },
        "notes": "Fee estimates from Bitcoin Core estimatesmartfee (economical mode) for a confirmation "
                 "target in blocks; sat/vB. Computed by AurAgrid's own node.",
    }


def fetch(rpc=None) -> dict:
    return build_snapshot(rpc or BitcoinRPC())


feeds.describe(
    "btc-fees", source="AurAgrid's own Bitcoin Core node (mainnet)",
    url="bitcoin-core-rpc:estimatesmartfee,getmempoolinfo,getblockstats",
    terms_url="/terms",
    attribution="Computed by AurAgrid from its own Bitcoin Core node.",
    as_of=lambda snap: snap["tip"]["block_time_utc"])
