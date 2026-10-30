#!/bin/bash
# AurAgrid backend: loopback only, reachable through Aperture.
cd /home/ubuntu/AurAgrid || exit 1
eval "$(grep '^export AURAGRID_' /home/ubuntu/.bashrc)"
export AURAGRID_GATEWAY_SECRET="$(cat gateway.secret)"
# Feed credentials (KEY=value lines, chmod 600, gitignored)
if [ -f feeds.env ]; then set -a; . ./feeds.env; set +a; fi
exec /home/ubuntu/miniconda/envs/auragrid_env/bin/uvicorn app.main:app --host 127.0.0.1 --port 8001
