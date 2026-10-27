import json
import hashlib
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler

class L402APIHandler(BaseHTTPRequestHandler):
    def _send_response(self, code, body, headers=None):
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        if headers:
            for k, v in headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode('utf-8'))

    def do_GET(self):
        auth_header = self.headers.get('Authorization', '')
        if auth_header.startswith('L402 '):
            try:
                token = auth_header.split(' ')[1]
                preimage, r_hash = token.split(':')
                computed_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
                if computed_hash == r_hash:
                    data = {
                        "status": "200 OK",
                        "endpoint": self.path,
                        "telemetry": {
                            "grid_frequency_hz": 50.02,
                            "voltage_v": 230.4,
                            "load_kw": 1420.5,
                            "solar_output_kw": 850.0,
                            "grid_status": "STABLE"
                        },
                        "l402_verified": True
                    }
                    return self._send_response(200, data)
            except Exception:
                pass

        amt = 100 if "analytics" in self.path else 10
        memo = f"AurAgrid L402 Access: {self.path}"
        try:
            cmd = ["sudo", "docker", "exec", "auragrid-lnd-2", "lncli", "--network=mainnet", "addinvoice", f"--amt={amt}", f"--memo={memo}"]
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            inv_data = json.loads(res.stdout)
            pay_req = inv_data.get("payment_request")
            r_hash = inv_data.get("r_hash", inv_data.get("payment_hash"))
            response_body = {
                "error": "L402 Payment Required",
                "price_sats": amt,
                "payment_request": pay_req,
                "r_hash": r_hash,
                "instructions": "Pay invoice and send Authorization: L402 <preimage>:<r_hash>"
            }
            headers = {"WWW-Authenticate": f'L402 invoice="{pay_req}"'}
            return self._send_response(402, response_body, headers)
        except Exception as e:
            return self._send_response(500, {"error": f"Node 2 Error: {str(e)}"})

def run_server():
    server = HTTPServer(('0.0.0.0', 8000), L402APIHandler)
    print("⚡ AurAgrid L402 API Server running on port 8000...")
    server.serve_forever()

if __name__ == '__main__':
    run_server()
