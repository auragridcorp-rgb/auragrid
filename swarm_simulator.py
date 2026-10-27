import requests
import time
import random

endpoints = [
    "/api/v1/esg/carbon", "/api/v1/energy/grid", "/api/v1/sar/commodities", "/api/v1/maritime/congestion",
    "/api/v1/weather/micro-climate", "/api/v1/logistics/cross-border", "/api/v1/wholesale/hyperscaler-power",
    "/api/v1/wholesale/lng-supply", "/api/v1/wholesale/crop-moisture", "/api/v1/wholesale/sts-anomalies",
    "/api/v1/wholesale/weather-routing", "/api/v1/wholesale/curtailment-risk", "/api/v1/wholesale/dark-fleet",
    "/api/v1/wholesale/ercot-lmp", "/api/v1/wholesale/port-congestion-index", "/api/v1/wholesale/carbon-credit-audit",
    "/api/v1/wholesale/spr-stockpile", "/api/v1/wholesale/critical-minerals", "/api/v1/wholesale/naval-escalation",
    "/api/v1/wholesale/wildfire-carbon", "/api/v1/wholesale/green-compute", "/api/v1/wholesale/agri-yield"
]

print("🤖 Launching simulated AI Swarm against Aperture Paywall...")
for _ in range(3): # Loop a few times to simulate heavy traffic
    random.shuffle(endpoints)
    for ep in endpoints:
        try:
            # Hit the Aperture reverse-proxy on 8080
            res = requests.get(f"http://127.0.0.1:8080{ep}", timeout=2)
            if res.status_code == 402:
                print(f"⚡ Paywall triggered: {ep} -> LND Invoice Generated")
            time.sleep(random.uniform(0.1, 0.5))
        except Exception as e:
            pass
print("✅ Swarm run complete. Check your dashboard!")
