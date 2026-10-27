from auragrid_mcp.client import AurAgridClient
import time

# Initialize SDK with Agent's Lightning Wallet credentials
client = AurAgridClient(macaroon_hex="YOUR_MACAROON_HEX", tls_cert="tls.cert")

print("🔍 AurAgrid ESG Auditor Bot Online")
while True:
    print("Requesting Carbon Audit Matrix...")
    # The SDK handles the 402 payment automatically
    data = client.fetch("/api/v1/wholesale/carbon-credit-audit")
    print(f"Data acquired: {data}")
    time.sleep(3600)
