import requests
import os
import time
import base64
import json
import subprocess

APERTURE_PROXY = "http://127.0.0.1:8080"
ARBITRAGE_FEED_URL = f"{APERTURE_PROXY}/api/v1/global/carbon/arbitrage-alerts"

def execute_arbitrage_query():
    print("🤖 [Arbitrage Swarm] Fetching high-value carbon intelligence feed...")
    try:
        res = requests.get(ARBITRAGE_FEED_URL)
        print("Received data without payment:", res.text)
    except requests.exceptions.HTTPError as e:
        pass
    except Exception as e:
        # Initial request will trigger 402 via Aperture
        pass

    # Direct L402 challenge loop
    try:
        req = requests.get(ARBITRAGE_FEED_URL)
        if req.status_code == 402:
            www_auth = req.headers.get("WWW-Authenticate", "")
            print(f"⚡ [Arbitrage Swarm] Caught 402 Challenge: {www_auth}")
            
            # Request invoice directly from proxy or parse from response
            res_body = req.json()
            pay_req = res_body.get("payment_request")
            r_hash = res_body.get("r_hash")
            
            if not pay_req:
                print("❌ No payment request found in 402 response.")
                return

            print(f"💳 Paying 500 sats for arbitrage alert via Node 2...")
            pay_cmd = [
                "sudo", "docker", "exec", "auragrid-lnd-2", "lncli",
                "--network=mainnet", "payinvoice", "--force", "--json", pay_req
            ]
            
            pay_res = subprocess.run(pay_cmd, capture_output=True, text=True, check=True)
            pay_data = json.loads(pay_res.stdout)
            
            preimage = pay_data.get("payment_preimage")
            print(f"✅ Payment successful! Preimage obtained.")
            
            auth_header = f"L402 {preimage}:{r_hash}"
            auth_res = requests.get(ARBITRAGE_FEED_URL, headers={"Authorization": auth_header})
            
            print("\n🎉 [Arbitrage Swarm] PREMIUM ARBITRAGE INTEL SECURED!")
            print(json.dumps(auth_res.json(), indent=2))
            
    except Exception as err:
        print(f"⚠️ Arbitrage Swarm execution error: {err}")

if __name__ == '__main__':
    execute_arbitrage_query()
