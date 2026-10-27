import requests
import re

class AurAgridClient:
    def __init__(self, base_url: str = "http://localhost:8000", pay_callback=None):
        self.base_url = base_url.rstrip("/")
        self.pay_callback = pay_callback

    def _request(self, endpoint: str, params: dict = None):
        url = f"{self.base_url}{endpoint}"
        response = requests.get(url, params=params)

        # 1. Check for Aperture L402 Paywall
        if response.status_code == 402:
            auth_header = response.headers.get("WWW-Authenticate", "")
            
            # Extract macaroon and invoice from header
            macaroon_match = re.search(r'macaroon="([^"]+)"', auth_header)
            invoice_match = re.search(r'invoice="([^"]+)"', auth_header)
            
            if macaroon_match and invoice_match:
                macaroon = macaroon_match.group(1)
                invoice = invoice_match.group(1)
            else:
                raise ValueError(f"Could not parse L402 details from header: {auth_header}")

            if self.pay_callback:
                preimage = self.pay_callback(invoice)
            else:
                raise ValueError("L402 Payment Error: No pay_callback provided to settle Lightning invoice.")

            # Re-request with the paid L402 token
            headers = {"Authorization": f"L402 {macaroon}:{preimage}"}
            response = requests.get(url, params=params, headers=headers)

        # 2. Graceful JSON parsing
        try:
            return response.json()
        except ValueError:
            return f"\n⚠️ Non-JSON Response (Status {response.status_code}):\n{response.text}"

    # Core Telemetry
    def get_esg_carbon(self, location_id: str = "global-1"):
        return self._request("/api/v1/esg/carbon", params={"location_id": location_id})

    def get_energy_grid(self, region: str = "US-TEXAS-ERCOT"):
        return self._request("/api/v1/energy/grid", params={"region": region})

    def get_sar_commodities(self, target: str = "crude_oil_vessel_tankers"):
        return self._request("/api/v1/sar/commodities", params={"target": target})

    # High-Value Niche Feeds
    def get_maritime_congestion(self, chokepoint: str = "strait_of_malacca"):
        return self._request("/api/v1/maritime/congestion", params={"chokepoint": chokepoint})

    def get_weather_microclimate(self, zone: str = "ercot_west_wind_belt"):
        return self._request("/api/v1/weather/micro-climate", params={"zone": zone})

    def get_logistics_cross_border(self, facility_id: str = "fab_semiconductor_taiwan_01"):
        return self._request("/api/v1/logistics/cross-border", params={"facility_id": facility_id})

if __name__ == "__main__":
    import sys

    def mock_wallet_callback(invoice):
        print(f"\n🛑 [402 PAYMENT REQUIRED] Aperture successfully intercepted the request!")
        print(f"⚡ BOLT11 Invoice generated:\n{invoice}\n")
        print("🤖 In a live scenario, the AI agent's Lightning wallet would automatically pay this.")
        print("✅ Test complete: L402 paywall is active and protecting the endpoint!")
        sys.exit(0)

    print("🚀 Initiating Client Test...")
    # Pointing to Port 8080 to hit the Aperture proxy
    client = AurAgridClient(base_url="http://localhost:8080", pay_callback=mock_wallet_callback)

    print("📡 Fetching Energy Grid telemetry for 'US-TEXAS-ERCOT'...")
    try:
        result = client.get_energy_grid()
        print("Result:", result)
    except Exception as e:
        print(f"Error: {e}")
