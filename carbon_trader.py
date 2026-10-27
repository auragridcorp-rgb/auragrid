import requests
import base64
import os
import time
import json
import subprocess

EXCHANGE_URL = "http://127.0.0.1:8080/api/v1/grid/buy-carbon-offset"

def execute_carbon_offset_purchase():
    print("\n[TRADING BOT] 💸 Initiating institutional carbon offset trade (50,000 sats)...")
    
    try:
        req = requests.get(EXCHANGE_URL)
        if req.status_code == 402:
            res_body = req.json()
            pay_req = res_body.get("payment_request")
            r_hash = res_body.get("r_hash")
            
            print(f"💳 Whale Invoice received: 50,000 sats")
            
            pay_cmd = [
                "sudo", "docker", "exec", "auragrid-lnd-2", "lncli",
                "--network=mainnet", "payinvoice", "--force", "--json", pay_req
            ]
            
            pay_res = subprocess.run(pay_cmd, capture_output=True, text=True, check=True)
            pay_data = json.loads(pay_res.stdout)
            
            if "payment_error" in pay_data and pay_data["payment_error"]:
                print(f"[ERROR] Trade execution failed: {pay_data['payment_error']}")
                return

            preimage = pay_data.get("payment_preimage")
            print(f"✅ Capital transmitted! Preimage: {preimage[:16]}...")
            
            auth_header = f"L402 {preimage}:{r_hash}"
            auth_res = requests.get(EXCHANGE_URL, headers={"Authorization": auth_header})
            
            print("\n🎉 [TRADING BOT] CARBON OFFSET SETTLEMENT COMPLETE!")
            print(json.dumps(auth_res.json(), indent=2))
            
    except Exception as e:
        print(f"[ERROR] Trade execution connection failed: {e}")

if __name__ == '__main__':
    execute_carbon_offset_purchase()
