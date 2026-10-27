#!/usr/bin/env python3
import json
import subprocess
import logging
from datetime import datetime

# Setup Logging
logging.basicConfig(
    filename="/home/ubuntu/AurAgrid/logs/sweeper.log",
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

VAULT_ADDRESS = "bc1ptrfpsag2t7w5fymy94fqm3uu5wnryurjh3sulcjca8tdryuxljlqup6q27"
BUFFER_SATS = 1000000        # Keep 1M sats local operational balance
MIN_SWEEP_THRESHOLD = 250000 # Minimum excess required to trigger on-chain sweep

def run_sweep():
    try:
        # Check current channel balance
        res = subprocess.run(["lncli", "channelbalance"], capture_output=True, text=True, check=True)
        data = json.loads(res.stdout)
        local_balance = int(data.get("local_balance", {}).get("sat", 0))

        excess = local_balance - BUFFER_SATS

        logging.info(f"Local Balance: {local_balance:,} sats | Operational Buffer: {BUFFER_SATS:,} sats")

        if excess >= MIN_SWEEP_THRESHOLD:
            logging.info(f"Target threshold met. Initiating sweep of {excess:,} sats to self-custodial vault...")
            
            sweep_cmd = [
                "lncli", "sendcoins",
                f"--addr={VAULT_ADDRESS}",
                f"--amt={excess}"
            ]
            
            sweep_res = subprocess.run(sweep_cmd, capture_output=True, text=True)
            if sweep_res.returncode == 0:
                logging.info(f"SUCCESS: Sweep executed! Output: {sweep_res.stdout.strip()}")
            else:
                logging.error(f"FAILED: Could not execute sweep. Error: {sweep_res.stderr.strip()}")
        else:
            logging.info(f"No sweep needed. Excess ({excess:,} sats) is below minimum threshold ({MIN_SWEEP_THRESHOLD:,} sats).")

    except Exception as e:
        logging.error(f"Unexpected error in sweeper routine: {str(e)}")

if __name__ == "__main__":
    run_sweep()
