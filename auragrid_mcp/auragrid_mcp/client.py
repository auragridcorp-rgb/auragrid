import requests
import re
import base64

class AurAgridClient:
    def __init__(self, proxy_url="http://84.12.79.124:8080", lnd_rest_url="https://127.0.0.1:8084", macaroon_hex="", tls_cert=""):
        self.proxy_url = proxy_url
        self.lnd_url = lnd_rest_url
        self.macaroon_hex = macaroon_hex
        self.tls_cert = tls_cert

    def _pay_invoice(self, invoice):
        headers = {"Grpc-Metadata-macaroon": self.macaroon_hex}
        res = requests.post(f"{self.lnd_url}/v1/channels/transactions", headers=headers, json={"payment_request": invoice}, verify=self.tls_cert)
        preimage_b64 = res.json().get("payment_preimage")
        return base64.b64decode(preimage_b64).hex() if preimage_b64 else None

    def fetch(self, endpoint):
        target = f"{self.proxy_url}{endpoint}"
        res = requests.get(target)
        if res.status_code == 402:
            auth_header = res.headers.get("Www-Authenticate", "")
            macaroon = re.search(r'macaroon="(.*?)"', auth_header).group(1)
            invoice = re.search(r'invoice="(.*?)"', auth_header).group(1)
            
            preimage = self._pay_invoice(invoice)
            if preimage:
                return requests.get(target, headers={"Authorization": f"L402 {macaroon}:{preimage}"}).json()
        return res.json() if res.status_code == 200 else {"error": res.status_code}
