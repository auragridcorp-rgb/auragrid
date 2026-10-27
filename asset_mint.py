import subprocess
import json

def mint_carbon_asset():
    print("=====================================================")
    print("[AURAGRID BROKERAGE] Minting Tokenized Carbon Credits...")
    
    # Command to mint 1,000 units of 'JINJACARBON' via Taproot Assets CLI (tapcli)
    mint_command = [
        "tapcli", "assets", "mint",
        "--type", "normal",
        "--name", "JINJACARBON",
        "--supply", "1000",
        "--meta_bytes", "Bujagali & Isimba Hydro Cascade Offset v1.0",
        "--group_key"
    ]
    
    try:
        result = subprocess.run(mint_command, capture_output=True, text=True, check=True)
        print("[SUCCESS] Asset Mint Initiated:")
        print(result.stdout)
        
        # Finalize the minting batch
        print("[AURAGRID BROKERAGE] Finalizing batch on-chain...")
        finalize_command = ["tapcli", "assets", "mint", "finalize"]
        fin_result = subprocess.run(finalize_command, capture_output=True, text=True, check=True)
        print(fin_result.stdout)
        
    except subprocess.CalledProcessError as e:
        print(f"[ERROR] Minting failed: {e.stderr}")

if __name__ == "__main__":
    mint_carbon_asset()