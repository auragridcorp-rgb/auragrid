import requests
import time
import json
import base64
import os
import urllib3

# Suppress local TLS certificate warnings for Polar
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configuration (Dave's Wallet)
LND_REST_URL = "https://127.0.0.1:8084"
MACAROON_PATH = r"C:\Users\user\.polar\networks\1\volumes\lnd\dave\data\chain\bitcoin\regtest\admin.macaroon"
TLS_CERT_PATH = r"C:/Users/user/.polar/networks/1/volumes/lnd/dave/tls.cert"

# Premium Target Endpoint behind Aperture Proxy
PROXY_URL = "http://127.0.0.1:8080/api/v1/global/carbon/undervalued"

class L402Vault:
    def __init__(self, lnd_url, macaroon_path, tls_cert, max_daily_sats=100000):
        self.lnd_url = lnd_url
        self.macaroon_path = macaroon_path
        self.tls_cert = tls_cert
        self.max_daily_sats = max_daily_sats
        self.sats_spent_today = 0
        self.token_cache = {}  # {url: "L402 <macaroon>:<preimage>"}

    def get_macaroon_hex(self):
        if not os.path.exists(self.macaroon_path):
            print(f"\n[ERROR] Macaroon not found at {self.macaroon_path}")
            return None
        with open(self.macaroon_path, "rb") as f:
            return f.read().hex()

    def pay_invoice(self, invoice):
        print(f"[ESG BOT] Authorizing micro-payment for institutional compliance data...")
        mac_hex = self.get_macaroon_hex()
        if not mac_hex:
            return None

        headers = {"Grpc-Metadata-macaroon": mac_hex}
        payload = {"payment_request": invoice}
        url = f"{self.lnd_url}/v1/channels/transactions"
        
        try:
            res = requests.post(url, headers=headers, json=payload, verify=self.tls_cert)
            response_data = res.json()
            
            if "payment_error" in response_data and response_data["payment_error"]:
                print(f"[ERROR] Payment failed: {response_data['payment_error']}")
                return None
                
            preimage_b64 = response_data.get("payment_preimage")
            if preimage_b64:
                return base64.b64decode(preimage_b64).hex()
            return None
        except Exception as e:
            print(f"[ERROR] Payment connection failed: {e}")
            return None

    def secure_request(self, url, estimated_sats=250):
        # 1. CACHE CHECK: Use active token if available
        if url in self.token_cache:
            l402_token = self.token_cache[url]
            print("⚡ [PROXY] Transmitting Cached L402 Cryptographic Proof...")
            res = requests.get(url, headers={"Authorization": l402_token})
            if res.status_code == 200:
                print("⚡ [ESG BOT] Access granted instantly via cached L402 proof (0 Sats spent).")
                return res.json()
            print("⚠️ [CACHE EXPIRED] Token invalid or expired. Renegotiating challenge...")
            del self.token_cache[url]

        # 2. INITIAL HANDSHAKE
        res = requests.get(url)
        if res.status_code == 200:
            return res.json()

        if res.status_code == 402:
            auth_header = res.headers.get("Www-Authenticate", "")
            print("[PROXY] 🔒 402 Payment Required intercepted.")
            
            try:
                macaroon = auth_header.split('macaroon="')[1].split('"')[0]
                invoice = auth_header.split('invoice="')[1].split('"')[0]
            except IndexError:
                print("[ERROR] Could not parse L402 challenge header.")
                return None

            # 3. CIRCUIT BREAKER
            if self.sats_spent_today + estimated_sats > self.max_daily_sats:
                print(f"🚨 [CIRCUIT BREAKER] Daily spend limit ({self.max_daily_sats} Sats) reached. Halting audit.")
                return None

            # 4. PAY & CACHE
            preimage = self.pay_invoice(invoice)
            if preimage:
                self.sats_spent_today += estimated_sats
                print(f"[WALLET] Payment Cleared! Preimage secured: {preimage[:15]}...")
                print(f"📊 [BUDGET TELEMETRY] Sats Spent Today: {self.sats_spent_today} / {self.max_daily_sats} Sats")
                
                l402_token = f"L402 {macaroon}:{preimage}"
                self.token_cache[url] = l402_token
                
                print("[PROXY] Transmitting L402 Cryptographic Proof...")
                final_res = requests.get(url, headers={"Authorization": l402_token})
                if final_res.status_code == 200:
                    return final_res.json()
                else:
                    print(f"[ERROR] Proof rejected by server. Status: {final_res.status_code}")
        else:
            print(f"[ERROR] Expected 402 challenge, received status: {res.status_code}")
        return None

def audit_carbon_oracle(vault: L402Vault):
    print("=====================================================")
    print("[ESG BOT] Initiating Global Carbon Intelligence Audit...")
    
    data = vault.secure_request(PROXY_URL, estimated_sats=250)
    
    if data:
        print("\n[ESG BOT] ✅ GLOBAL AUDIT SUCCESSFUL. DATA UNLOCKED:")
        print(json.dumps(data, indent=2))
        
        opportunities = data.get("opportunities", [])
        print(f"\n[AUDIT SUMMARY] Evaluated {len(opportunities)} Global Carbon Opportunities:")
        for opp in opportunities:
            print(f"  -> Project {opp['project_id']} ({opp['registry']}): {opp['discount_percentage']}% below fair value")

if __name__ == "__main__":
    # Vault initialized outside the loop so cache & budget persist across cycles
    vault = L402Vault(LND_REST_URL, MACAROON_PATH, TLS_CERT_PATH, max_daily_sats=100000)
    
    while True:
        try:
            audit_carbon_oracle(vault)
            time.sleep(15)
        except KeyboardInterrupt:
            print("\n[ESG BOT] Audit stopped by user.")
            break
        except Exception as e:
            print(f"[ERROR] Audit cycle exception: {e}")
            time.sleep(15)