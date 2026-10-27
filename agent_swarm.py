import requests
import time
import json
import random
import base64
import re
import urllib3
import os

# Suppress local TLS certificate warnings for Polar
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ==========================================
# AGENT CONFIGURATION
# ==========================================
MANIFEST_URL = "http://auragrid_api:8000/.well-known/auragrid-agent.json"
APERTURE_PROXY = "http://127.0.0.1:8080"

# The Agent needs its own Lightning Node to pay invoices automatically.
# In Polar, you typically have an "Alice" node (AurAgrid) and a "dave" node.
# We will use dave's node to act as the AI Agent's wallet.
# Update the paths below if your dave node has a different number in the path (e.g., networks/1/... )
AGENT_LND_REST = "None # Revenue-focused outbound sales agent" 
TLS_CERT = r"C:/Users/user/.polar/networks/1/volumes/lnd/dave/tls.cert"
MACAROON_PATH = r"C:\Users\user\.polar\networks\1\volumes\lnd\dave\data\chain\bitcoin\regtest\admin.macaroon"

def get_macaroon_hex():
    """Reads the LND admin macaroon and converts it to hex format for the REST header."""
    if not os.path.exists(MACAROON_PATH):
        print(f"\n[ERROR] Agent Wallet Macaroon not found at {MACAROON_PATH}")
        print("Please ensure your Polar 'dave' node is running and the path is correct.")
        exit(1)
        
    with open(MACAROON_PATH, 'rb') as f:
        macaroon_bytes = f.read()
    return macaroon_bytes.hex()

def pay_lightning_invoice(invoice_str):
    """Programmatically pays a BOLT11 invoice using the Agent's LND node."""
    print(f"   [WALLET] Authorizing payment for invoice: {invoice_str[:15]}...{invoice_str[-10:]}")
    
    headers = {"Grpc-Metadata-macaroon": get_macaroon_hex()}
    data = {"payment_request": invoice_str}
    
    url = f"{AGENT_LND_REST}/v1/channels/transactions"
    
    try:
        response = requests.post(url, headers=headers, json=data, verify=TLS_CERT)
        response_data = response.json()
        
        if "payment_error" in response_data and response_data["payment_error"]:
            print(f"   [ERROR] Payment failed: {response_data['payment_error']}")
            return None
            
        # LND REST returns the preimage as base64, we need it as a hex string for L402
        preimage_b64 = response_data.get("payment_preimage")
        if preimage_b64:
            preimage_hex = base64.b64decode(preimage_b64).hex()
            print(f"   [WALLET] Payment Cleared! Preimage secured: {preimage_hex[:15]}...")
            return preimage_hex
        else:
            print("   [ERROR] No preimage returned from LND.")
            return None
            
    except Exception as e:
        print(f"   [FATAL] Failed to connect to Agent's Lightning Wallet: {e}")
        return None

def run_agent_swarm():
    print("==================================================")
    print("🤖 AURAGRID AUTONOMOUS M2M AGENT SWARM INITIALIZED")
    print("==================================================")
    
    # 1. Discover Endpoints
    print("\n[AGENT] Crawling for AurAgrid Manifest...")
    try:
        manifest_res = requests.get(MANIFEST_URL)
        manifest = manifest_res.json()
        endpoints = [node["endpoint"] for node in manifest["nodes"]]
        print(f"[AGENT] Manifest acquired! Found {len(endpoints)} L402-gated data routes.")
    except Exception as e:
        print(f"[ERROR] Could not fetch manifest: {e}")
        return

    # 2. Infinite Autonomous Trading Loop
    while True:
        target_endpoint = random.choice(endpoints)
        target_url = f"{APERTURE_PROXY}{target_endpoint}"
        
        print(f"\n--------------------------------------------------")
        print(f"[AGENT] Requesting Telemetry: {target_endpoint}")
        
        # Step A: Attempt to access without paying
        res = requests.get(target_url)
        
        if res.status_code == 402:
            print("   [PROXY] 🔒 402 Payment Required intercepted.")
            auth_header = res.headers.get("Www-Authenticate", "")
            
            # Regex parse the L402 challenge
            macaroon_match = re.search(r'macaroon="(.*?)"', auth_header)
            invoice_match = re.search(r'invoice="(.*?)"', auth_header)
            
            if macaroon_match and invoice_match:
                macaroon = macaroon_match.group(1)
                invoice = invoice_match.group(1)
                
                # Step B: Pay the invoice automatically
                preimage = pay_lightning_invoice(invoice)
                
                if preimage:
                    # Step C: Re-request the data with the cryptographic proof
                    l402_header = f"L402 {macaroon}:{preimage}"
                    print("   [PROXY] Transmitting L402 Cryptographic Proof...")
                    
                    paid_res = requests.get(target_url, headers={"Authorization": l402_header})
                    
                    if paid_res.status_code == 200:
                        print("   [PROXY] ✅ Access Granted!")
                        data = paid_res.json()
                        print(f"   [DATA] {json.dumps(data, indent=2)}")
                    else:
                        print(f"   [ERROR] Proof rejected. Status: {paid_res.status_code}")
            else:
                print("   [ERROR] Could not parse L402 challenge.")
                
        time.sleep(5) # Pause 5 seconds before the next autonomous request

if __name__ == "__main__":
    run_agent_swarm()
