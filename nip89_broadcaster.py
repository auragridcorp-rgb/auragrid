"""Publish the AurAgrid DVM's NIP-89 announcement (kind 31990) once.

Uses the DVM's own key (dvm/dvm_key.txt, or AURAGRID_DVM_KEY_FILE) and the live
manifest, so the announcement matches what the DVM actually serves. The DVM
worker also republishes it on every relay connection; this is for a manual push.

    python nip89_broadcaster.py
"""
import asyncio
import json

import httpx
import websockets

from dvm import nip90_worker as dvm


async def main():
    key = dvm.load_key()
    async with httpx.AsyncClient(timeout=15) as http:
        manifest = (await http.get(dvm.BACKEND + "/agent-manifest.json")).json()
    event = dvm.announcement(key, manifest)
    print("DVM", key.public_key.bech32(), "announcing:", [t[1] for t in event["tags"] if t[0] == "t"])
    for relay in dvm.RELAYS:
        try:
            async with websockets.connect(relay, open_timeout=10) as ws:
                await ws.send(json.dumps(["EVENT", event]))
                print("  %s -> %s" % (relay, await asyncio.wait_for(ws.recv(), timeout=10)))
        except Exception as exc:
            print("  %s failed: %s" % (relay, exc))


if __name__ == "__main__":
    asyncio.run(main())
