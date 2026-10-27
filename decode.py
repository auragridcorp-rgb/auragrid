import base64
from pymacaroons import Macaroon

# Your full LSAT token captured from the backend terminal
lsat_token = "AgEEbHNhdAJCAAAXZLN+wy7uubC0vw+sn9x85xxge6+NNYA6kvb2RtrBww2XCpeFAY9SZtwcIalo/Dl0IsaL6w7dl7MBMOL3p1smAAIUc2VydmljZXM9dGVsZW1ldHJ5OjAAAhd0ZWxlbWV0cnlfY2FwYWJpbGl0aWVzPQAABiAMSkSThSLP9OAVl6XNr9yel0dZJIdM5nAfPSHRTakoNg==:d2f1718b08c2bb49f9213b6ca14579698531754d3f1f89fcf832396f72818aca"

def run_decoder():
    try:
        # Separate the LSAT token into Macaroon and Preimage parts by splitting at the colon
        parts = lsat_token.strip().split(":")
        macaroon_part = parts[0]
        
        if macaroon_part.startswith("LSAT "):
            macaroon_part = macaroon_part[5:]
            
        # Ensure correct base64 padding length
        missing_padding = len(macaroon_part) % 4
        if missing_padding:
            macaroon_part += '=' * (4 - missing_padding)
            
        # Decode base64 safely into raw bytes
        macaroon_bytes = base64.urlsafe_b64decode(macaroon_part.encode('utf-8'))
        
        # Deserialize the binary macaroon structure
        m = Macaroon.deserialize(macaroon_bytes)
        
        print("========================================")
        print("SUCCESS: L402 Macaroon Decoded!")
        print(f"Location: {m.location}")
        print(f"Identifier (Hex): {m.identifier.hex() if isinstance(m.identifier, bytes) else m.identifier}")
        print("\n--- Embedded Caveats (Rules/Permissions) ---")
        
        for c in m.caveats:
            cid = c.caveat_id
            if isinstance(cid, bytes):
                try:
                    print(f"  - {cid.decode('utf-8')}")
                except Exception:
                    print(f"  - [Binary Data Hex]: {cid.hex()}")
            else:
                print(f"  - {cid}")
        print("========================================")
                
    except Exception as e:
        print(f"DECODE ERROR: {e}")

if __name__ == "__main__":
    run_decoder()