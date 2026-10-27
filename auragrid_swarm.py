import urllib.request
import urllib.parse
import json
import subprocess
import time

API_URL = "http://localhost:8001/api/v1/grid/live-status"
TELEGRAM_BOT_TOKEN = "REMOVED_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID = "REMOVED_TELEGRAM_CHAT_ID"

def send_telegram_alert(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode('utf-8')
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req) as response:
            pass
    except Exception as e:
        print(f"Telegram Alert Error: {e}")

def execute_swarm_purchase(max_retries=3):
    print("🤖 [Swarm Agent] Requesting energy grid telemetry...")
    
    for attempt in range(1, max_retries + 1):
        try:
            req = urllib.request.Request(API_URL)
            response = urllib.request.urlopen(req)
            print("Received data without payment:", response.read().decode('utf-8'))
            return
        except urllib.error.HTTPError as e:
            if e.code == 402:
                print(f"⚡ [Swarm Agent] HTTP 402 Payment Required (Attempt {attempt}/{max_retries})")
                res_body = json.loads(e.read().decode('utf-8'))
                pay_req = res_body.get("payment_request")
                r_hash = res_body.get("r_hash")
                price = res_body.get("price_sats")
                
                print(f"💳 Invoice: {price} sats | Hash: {r_hash[:16]}...")
                
                pay_cmd = [
                    "sudo", "docker", "exec", "auragrid-lnd-2", "lncli",
                    "--network=mainnet", "payinvoice", "--force", "--json", pay_req
                ]
                
                try:
                    pay_res = subprocess.run(pay_cmd, capture_output=True, text=True, check=True)
                    pay_data = json.loads(pay_res.stdout)
                    
                    if "payment_error" in pay_data and pay_data["payment_error"]:
                        raise Exception(pay_data["payment_error"])
                        
                    preimage = pay_data.get("payment_preimage")
                    print(f"✅ [Swarm Agent] Payment Successful! Preimage: {preimage[:16]}...")
                    
                    auth_header = f"L402 {preimage}:{r_hash}"
                    auth_req = urllib.request.Request(API_URL, headers={"Authorization": auth_header})
                    
                    data_res = urllib.request.urlopen(auth_req)
                    final_data = json.loads(data_res.read().decode('utf-8'))
                    
                    print("\n🎉 [Swarm Agent] DATA PURCHASE COMPLETE!")
                    print(json.dumps(final_data, indent=2))
                    return
                    
                except Exception as pay_err:
                    print(f"⚠️ [Swarm Agent] Payment attempt {attempt} failed: {pay_err}")
                    if attempt < max_retries:
                        sleep_time = 2 ** attempt
                        print(f"🔄 Retrying in {sleep_time} seconds...")
                        time.sleep(sleep_time)
                    else:
                        err_msg = f"🚨 AurAgrid Swarm Alert: L402 payment failed after {max_retries} attempts. Error: {str(pay_err)}"
                        send_telegram_alert(err_msg)
                        print("❌ [Swarm Agent] Max retries reached. Alert sent to Telegram.")
            else:
                print(f"❌ Unexpected HTTP Error: {e}")
                break

if __name__ == '__main__':
    execute_swarm_purchase()
