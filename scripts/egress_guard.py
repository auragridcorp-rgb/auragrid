#!/usr/bin/env python3
"""Keep outbound traffic inside OCI's Always Free allowance (10 TB/month).

Run from cron every 15 minutes. It adds up this calendar month's (UTC)
outbound bytes on the main network interface, surviving reboots, and:

  * logs a warning once past AURAGRID_EGRESS_WARN_TB (default 5 TB)
  * at AURAGRID_EGRESS_LIMIT_TB (default 8 TB) stops the public gateway
    (`systemctl stop aperture`) and writes egress_guard.tripped. It does not
    restart it by itself: once you have checked what happened, run
    `rm egress_guard.tripped && sudo systemctl start aperture`.

All outbound traffic counts (also LND), so the API is stopped first, being the
only part that grows with public demand. State: egress_state.json (gitignored).
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "egress_state.json")
TRIPPED = os.path.join(ROOT, "egress_guard.tripped")
TB = 1000 ** 4
WARN = float(os.environ.get("AURAGRID_EGRESS_WARN_TB", "5")) * TB
LIMIT = float(os.environ.get("AURAGRID_EGRESS_LIMIT_TB", "8")) * TB


def default_interface() -> str:
    with open("/proc/net/route") as fh:
        for line in fh.readlines()[1:]:
            fields = line.split()
            if fields[1] == "00000000":
                return fields[0]
    raise RuntimeError("no default route")


def tx_bytes(iface: str) -> int:
    with open("/sys/class/net/%s/statistics/tx_bytes" % iface) as fh:
        return int(fh.read())


def boot_id() -> str:
    with open("/proc/sys/kernel/random/boot_id") as fh:
        return fh.read().strip()


def update(state: dict, counter: int, boot: str, month: str) -> dict:
    """Add the traffic since the last run to this month's total."""
    if state.get("boot") == boot and counter >= state.get("counter", 0):
        delta = counter - state.get("counter", 0)
    else:
        delta = counter   # rebooted (or counter reset): everything since boot is new
    total = (state.get("total", 0) if state.get("month") == month else 0) + delta
    return {"month": month, "total": total, "counter": counter, "boot": boot,
            "warned": state.get("warned", False) if state.get("month") == month else False}


def log(msg: str) -> None:
    print("%s egress_guard: %s" % (datetime.now(timezone.utc).isoformat(timespec="seconds"), msg), flush=True)


def main() -> int:
    try:
        with open(STATE) as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        state = {}
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    state = update(state, tx_bytes(default_interface()), boot_id(), month)

    if state["total"] >= LIMIT and not os.path.exists(TRIPPED):
        log("LIMIT: %.2f TB sent in %s (limit %.1f TB): stopping the public gateway"
            % (state["total"] / TB, month, LIMIT / TB))
        subprocess.run(["sudo", "-n", "systemctl", "stop", "aperture"], check=False)
        with open(TRIPPED, "w") as fh:
            fh.write("%s %.2f TB\n" % (month, state["total"] / TB))
    elif state["total"] >= WARN and not state["warned"]:
        log("WARNING: %.2f TB sent in %s (warning at %.1f TB, limit %.1f TB)"
            % (state["total"] / TB, month, WARN / TB, LIMIT / TB))
        state["warned"] = True

    tmp = STATE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh)
    os.replace(tmp, STATE)
    if "-v" in sys.argv:
        log("%.3f GB sent so far in %s" % (state["total"] / 1e9, month))
    return 0


if __name__ == "__main__":
    sys.exit(main())
