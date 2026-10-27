import requests
import time
import json
import base64
import re
import urllib3
import os

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

APERTURE_PROXY = "http://127.0.0.1:8080"
FEED_URL = f"{APERTURE_PROXY}/api/v1/global/carbon/arbitrage-alerts"

AGENT_LND_REST = "https://127.0.0.1:8084" 
TLS_CERT = r"C:/Users/user/.polar/networks/1/volumes/lnd/dave/tls.cert"
MACAROON_PATH = r"C:\Users\user\.polar\networks\1\volumes\lnd\dave\data\chain\bitcoin\regtest\admin.macaroon"

def get_macaroon_hex():
    with open(MACAROON_PATH, 'rb') as f:
        return f.read().hex()

def pay_invoice(invoice_str):
    headers = {"Grpc-Metadata-macaroon": get_macaroon_hex()}
    data = {"payment_request": invoice_str}
    try:
        res = requests.post(f"{AGENT_LND_REST}/v1/channels/transactions", headers=headers, json=data, verify=TLS_CERT)
        res_data = res.json()
        preimage_b64 = res_data.get("payment_preimage")
        if preimage_b64:
            return base64.b64decode(preimage_b64).hex()
    except Exception as e:
        print(f"[BEAR WALLET ERROR] {e}")
    return None

def run_bear_bot():
    print("🐻 [BEAR AGENT ONLINE] Risk-averse short-seller strategy active.")
    while True:
        print("\n[BEAR AGENT] Intercepting market feeds for high-risk flags...")
        res = requests.get(FEED_URL)
        if res.status_code == 402:
            auth_header = res.headers.get("Www-Authenticate", "")
            macaroon = re.search(r'macaroon="(.*?)"', auth_header).group(1)
            invoice = re.search(r'invoice="(.*?)"', auth_header).group(1)
            
            preimage = pay_invoice(invoice)
            if preimage:
                paid_res = requests.get(FEED_URL, headers={"Authorization": f"L402 {macaroon}:{preimage}"})
                if paid_res.status_code == 200:
                    data = paid_res.json()
                    print(f"🐻 [BEAR AGENT] Feed acquired. Analyzing execution risk: {data.get('execution_risk')}")
                    print("🐻 [BEAR AGENT] Strategy: Hedging position against market volatility. Standing down on execution.")
        time.sleep(35)

if __name__ == "__main__":
    run_bear_bot()