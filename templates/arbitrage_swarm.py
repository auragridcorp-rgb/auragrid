from auragrid_mcp.client import AurAgridClient
import time
import json

# Initialize SDK with Agent's Lightning Wallet credentials
client = AurAgridClient(macaroon_hex="YOUR_MACAROON_HEX", tls_cert="tls.cert")

print("📈 AurAgrid Arbitrage Swarm Online")
while True:
    print("Fetching ERCOT LMP pricing and Green Compute routing metrics...")
    
    # The SDK automatically handles the 402 L402 paywall for both endpoints seamlessly
    ercot_data = client.fetch("/api/v1/wholesale/ercot-lmp")
    compute_data = client.fetch("/api/v1/wholesale/green-compute")
    
    print("\n[ERCOT TELEMETRY]")
    print(json.dumps(ercot_data, indent=2))
    
    print("\n[COMPUTE ROUTING]")
    print(json.dumps(compute_data, indent=2))
    
    print("\nCalculating cross-border compute arbitrage spread... holding for next tick.")
    time.sleep(300) # Poll every 5 minutes
