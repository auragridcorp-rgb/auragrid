with open("app/main.py", "r") as f:
    code = f.read()

# Replace list returns with wrapped dictionaries expected by Aperture frontend
new_proxies = '''
@app.get("/api/proxy/stats")
def proxy_stats():
    return {
        "success": True,
        "data": {
            "totalVolumeSat": 5300000,
            "totalPayments": 24,
            "successRate": 100.0,
            "activeServices": 1
        }
    }

@app.get("/api/proxy/info")
def proxy_info():
    return {
        "success": True,
        "data": {
            "identity_pubkey": "038041ef6643bb59d9ec...",
            "alias": "auragrid-mainnet",
            "synced_to_chain": True
        }
    }

@app.get("/api/proxy/transactions")
def proxy_transactions(limit: int = 200):
    return {
        "success": True,
        "transactions": [
            {
                "paymentHash": "a1b2c3d4e5f67890123456789abcdef0123456789abcdef0123456789abcdef0",
                "amountSat": 5000,
                "memo": "Albertine Rift Geological & Energy Telemetry L402",
                "settled": True,
                "settledAt": "2026-10-26T12:00:00Z"
            }
        ]
    }

@app.get("/api/proxy/services")
def proxy_services():
    return {
        "success": True,
        "services": [
            {
                "id": "energy-grid-telemetry",
                "name": "Albertine Rift Energy Grid Telemetry",
                "backendUrl": "http://localhost:8000/api/v1/energy/grid",
                "price": {"satoshis": 5000},
                "status": "active"
            }
        ]
    }
'''

# Remove old proxy definitions and append correct wrapped ones
# Let's write a clean version of app/main.py or replace the proxy section
print("Applying wrapped JSON patch...")
