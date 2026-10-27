import asyncio
import json
import time
import httpx
import websockets
import random
import os
from nostr.event import Event
from nostr.key import PrivateKey

RELAYS = [
    "wss://relay.damus.io",
    "wss://nos.lol"
]

EXTERNAL_PAYWALL = "http://84.12.79.124:8080/api/v1"
LOCAL_PAYWALL = "http://127.0.0.1:8080/api/v1"

# The complete 22-endpoint institutional mapping
ROUTE_MAPPING = {
    "carbon": "/esg/carbon",
    "grid": "/energy/grid",
    "commodities": "/sar/commodities",
    "congestion": "/maritime/congestion",
    "microclimate": "/weather/micro-climate",
    "logistics": "/logistics/cross-border",
    "hyperscaler_power": "/wholesale/hyperscaler-power",
    "lng_supply": "/wholesale/lng-supply",
    "crop_moisture": "/wholesale/crop-moisture",
    "sts_anomalies": "/wholesale/sts-anomalies",
    "maritime_weather": "/wholesale/weather-routing",
    "renewable_curtailment": "/wholesale/curtailment-risk",
    "dark_fleet": "/wholesale/dark-fleet",
    "ercot_lmp": "/wholesale/ercot-lmp",
    "port_congestion": "/wholesale/port-congestion-index",
    "carbon_audit": "/wholesale/carbon-credit-audit",
    "spr_stockpile": "/wholesale/spr-stockpile",
    "critical_minerals": "/wholesale/critical-minerals",
    "naval_escalation": "/wholesale/naval-escalation",
    "wildfire_carbon": "/wholesale/wildfire-carbon",
    "green_compute": "/wholesale/green-compute",
    "agri_yield": "/wholesale/agri-yield"
}

# Persist identity so relays don't view us as a disposable bot
KEY_FILE = "dvm_key.txt"
if os.path.exists(KEY_FILE):
    with open(KEY_FILE, "r") as f:
        bot_key = PrivateKey(bytes.fromhex(f.read().strip()))
else:
    bot_key = PrivateKey()
    with open(KEY_FILE, "w") as f:
        f.write(bot_key.hex())

bot_pub = bot_key.public_key.hex()

def event_to_dict(evt):
    return {
        "id": evt.id, "pubkey": evt.public_key, "created_at": evt.created_at,
        "kind": evt.kind, "tags": evt.tags, "content": evt.content, "sig": evt.signature
    }

async def broadcast_dvm_manifest():
    """Globally announces all 22 endpoints to Nostr AI swarms (Kind 31990)."""
    print(f"[MANIFEST] Broadcasting AurAgrid 22-Feed Menu to Nostr relays...")
    content = json.dumps({
        "name": "AurAgrid L402 Oracle Node",
        "description": "Institutional AI Swarm Data Feeds (Retail & Wholesale). L402 strictly enforced.",
        "endpoints": list(ROUTE_MAPPING.keys())
    })
    tags = [["d", "auragrid-oracle-v2"], ["k", "5000"], ["t", "l402"], ["t", "wholesale_data"]]
    
    evt = Event(public_key=bot_pub, content=content, kind=31990, tags=tags, created_at=int(time.time()))
    bot_key.sign_event(evt)
    
    for relay in RELAYS:
        try:
            async with websockets.connect(relay) as ws:
                await ws.send(json.dumps(["EVENT", event_to_dict(evt)]))
                print(f"[MANIFEST] Success! Menu live on {relay}")
        except Exception:
            pass

async def handle_dvm_request(websocket, event_data):
    """Intercepts swarm queries and generates exact payload invoices."""
    try:
        event = event_data[2]
        kind = event.get("kind")
        if 5000 <= kind < 6000:
            job_id = event.get("id")
            target_topic = "carbon"
            for tag in event.get("tags", []):
                if len(tag) >= 2 and tag[0] in ["t", "param"] and tag[1] in ROUTE_MAPPING:
                    target_topic = tag[1]

            route = ROUTE_MAPPING.get(target_topic, "/esg/carbon")
            print(f"[LISTENER] Caught Job Request {job_id[:8]} for {target_topic.upper()}")

            async with httpx.AsyncClient() as client:
                paywall_resp = await client.get(f"{LOCAL_PAYWALL}{route}")

            if paywall_resp.status_code == 402:
                auth_header = paywall_resp.headers.get("www-authenticate", "")
                feedback_content = json.dumps({
                    "status": "payment_required",
                    "paywall_url": f"{EXTERNAL_PAYWALL}{route}",
                    "l402_header": auth_header
                })
                tags = [["e", job_id], ["p", event.get("pubkey")], ["status", "payment-required"]]
                
                reply_event = Event(
                    public_key=bot_pub, content=feedback_content, kind=7000, 
                    tags=tags, created_at=int(time.time())
                )
                bot_key.sign_event(reply_event)

                await websocket.send(json.dumps(["EVENT", event_to_dict(reply_event)]))
                print(f"[LISTENER] Issued premium L402 invoice for {target_topic.upper()}")

    except Exception as e:
        print(f"[LISTENER ERROR] {e}")

async def listen_relay(uri):
    while True:
        try:
            async with websockets.connect(uri) as websocket:
                sub = json.dumps(["REQ", "auragrid-listen", {"kinds": list(range(5000, 6000)), "limit": 10}])
                await websocket.send(sub)
                async for message in websocket:
                    data = json.loads(message)
                    if isinstance(data, list) and data[0] == "EVENT":
                        await handle_dvm_request(websocket, data)
        except Exception:
            await asyncio.sleep(10)

async def alpha_hunter():
    """Dynamically teases premium wholesale endpoints to generate FOMO."""
    await asyncio.sleep(5) 
    print(f"[HUNTER] Outbound Swarm Sales Agent active. Pubkey: {bot_pub[:8]}...")
    wholesale_keys = list(ROUTE_MAPPING.keys())[6:] # Focus marketing on 50k+ sat endpoints
    
    while True:
        try:
            target = random.choice(wholesale_keys)
            route = ROUTE_MAPPING[target]
            teaser = (
                f"⚡ AURAGRID INSTITUTIONAL SWARM ALERT ⚡\n"
                f"High-value telemetry matrix available: [{target.upper()}]\n"
                f"🔒 Unlock full JSON execution payload via L402:\n"
                f"GET {EXTERNAL_PAYWALL}{route}"
            )
            event = Event(public_key=bot_pub, content=teaser, kind=1, created_at=int(time.time()))
            bot_key.sign_event(event)

            for relay in RELAYS:
                try:
                    async with websockets.connect(relay) as ws:
                        await ws.send(json.dumps(["EVENT", event_to_dict(event)]))
                except Exception:
                    pass
            
            print(f"[HUNTER] Teased premium {target.upper()} matrix to network.")
            await asyncio.sleep(900)
        except Exception:
            await asyncio.sleep(60)

async def main():
    print("⚡ AurAgrid Dual-Threat Nostr Swarm Initializing...")
    await broadcast_dvm_manifest()
    tasks = [listen_relay(relay) for relay in RELAYS]
    tasks.append(alpha_hunter())
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
