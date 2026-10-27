import requests
import re

# The Aperture proxy endpoint
API_URL = "http://127.0.0.1:8080/api/v1/grid/hello"

def fetch_grid_data():
    print(f"[*] Sending initial request to {API_URL}...")
    
    # 1. Make the unauthenticated request
    response = requests.get(API_URL)

    # 2. Catch the L402 Paywall
    if response.status_code == 402:
        print("\n[!] 402 Payment Required intercepted.")
        auth_header = response.headers.get("Www-Authenticate", "")
        
        # 3. Parse the Macaroon and Invoice using Regex
        # Aperture returns both LSAT and L402 headers; we target L402
        macaroon_match = re.search(r'L402 macaroon="(.*?)",', auth_header)
        invoice_match = re.search(r'invoice="(.*?)"', auth_header)
        
        if macaroon_match and invoice_match:
            macaroon = macaroon_match.group(1)
            invoice = invoice_match.group(1)
            
            print(f"\n[+] L402 Challenge Parsed Successfully!")
            print(f"    Invoice: {invoice[:30]}...{invoice[-10:]}")
            
            # 4. Handle the Payment
            # In a production app, this connects to a Lightning Wallet API (WebLN) 
            # or LND REST to pay automatically in the background.
            print("\n[*] Please pay the invoice using your LND node:")
            print(f"    lncli payinvoice --force {invoice}")
            
            preimage = input("\n[>] Enter the payment preimage to continue: ").strip()
            
            # 5. Reconstruct the L402 Token
            l402_token = f"L402 {macaroon}:{preimage}"
            print("\n[*] Token constructed. Retrying request...")
            
            # 6. Retry the Request
            headers = {"Authorization": l402_token}
            final_response = requests.get(API_URL, headers=headers)
            
            print(f"\n[+] Success! Server returned HTTP {final_response.status_code}")
            print(f"[+] Data payload: {final_response.json()}")
            
        else:
            print("[-] Failed to parse L402 challenge from headers.")
            
    # Handle the case where the user already has a valid token session
    elif response.status_code == 200:
        print("[+] Success! No payment required.")
        print(f"[+] Data payload: {response.json()}")
    else:
        print(f"[-] Unexpected status code: {response.status_code}")

if __name__ == "__main__":
    fetch_grid_data()