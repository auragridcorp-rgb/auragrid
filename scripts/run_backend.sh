#!/bin/bash
# AurAgrid backend: loopback only, reachable through Aperture.
cd /home/ubuntu/AurAgrid || exit 1
eval "$(grep '^export AURAGRID_' /home/ubuntu/.bashrc)"
export AURAGRID_GATEWAY_SECRET="$(cat gateway.secret)"
# Dashboard password lives outside the repo and outside ~/.bashrc (rotated 2026-10-30)
export AURAGRID_ADMIN_PASSWORD="$(cat /home/ubuntu/.auragrid/admin_password)"
# v2 response signing via the production LND (macaroon is message:write only, chmod 600, gitignored)
export AURAGRID_ATTEST_MACAROON=/home/ubuntu/AurAgrid/attest.macaroon
export LND_REST_URL=https://127.0.0.1:8081
export LND_TLS_CERT=/home/ubuntu/.lnd/tls.cert
# Feed credentials (KEY=value lines, chmod 600, gitignored)
if [ -f feeds.env ]; then set -a; . ./feeds.env; set +a; fi
exec /home/ubuntu/miniconda/envs/auragrid_env/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
