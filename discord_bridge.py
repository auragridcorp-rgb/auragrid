import asyncio
import websockets
import httpx
import json
import os

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

async def discord_volatility_bridge():
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set.")
        return
    async for websocket in websockets.connect("ws://84.12.79.124:8000/ws/volatility"):
        try:
            async for message in websocket:
                data = json.loads(message)
                embed = {
                    "embeds": [{
                        "title": f"🚨 Volatility Alert: {data.get(\"stream\", \"Grid\")}",
                        "description": "Z-Score anomaly detected across telemetry matrix. Autonomous agent action recommended.",
                        "color": 16711680,
                        "fields": [
                            {"name": "L402 Endpoint", "value": f"`/api/v1/batch/{data.get(\"endpoint\", \"ercot-lmp\")}`"}
                        ]
                    }]
                }
                async with httpx.AsyncClient() as client:
                    await client.post(DISCORD_WEBHOOK_URL, json=embed)
        except (websockets.ConnectionClosed, Exception):
            await asyncio.sleep(5)
            continue

if __name__ == "__main__":
    asyncio.run(discord_volatility_bridge())
