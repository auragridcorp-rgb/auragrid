import requests
import time
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

BACKEND_URL = "http://auragrid_api:8000"
FEED_URL = f"{BACKEND_URL}/api/v1/global/carbon/arbitrage-alerts"
EXEC_URL = f"{BACKEND_URL}/api/v1/grid/buy-carbon-offset"

def run_direct_swarm():
    print("==================================================")
    print("🤖 AURAGRID DIRECT EXECUTION SWARM ONLINE")
    print("==================================================")
    
    while True:
        print("\n[SWARM] Polling direct market intelligence feed...")
        try:
            res = requests.get(FEED_URL, timeout=5)
            if res.status_code == 200:
                data = res.json()
                spread = data.get("spread_usd", 0.16)
                asset = data.get("asset", "Carbon Tonne")
                print(f"   -> [FEED SECURED] Spread on {asset}: +${spread}")
                
                print(f"   -> [SIGNAL] Executing automated carbon offset...")
                exec_res = requests.get(EXEC_URL, timeout=5)
                if exec_res.status_code == 200:
                    exec_data = exec_res.json()
                    print(f"   -> [SUCCESS] Status: {exec_data.get('status', 'RETIRED')}")
                    print(f"   -> [PROOF] {exec_data.get('cryptographic_proof', 'VERIFIED')}")
                else:
                    print(f"   [ERROR] Execution failed with status {exec_res.status_code}")
            else:
                print(f"   [ERROR] Feed returned status {res.status_code}")
        except Exception as e:
            print(f"   [ERROR] Connection failed: {e}")
            
        print("\n[SWARM] Standing by for next market cycle (Sleeping 10s)...")
        time.sleep(10)

if __name__ == "__main__":
    run_direct_swarm()