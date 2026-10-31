#!/bin/bash
# AurAgrid fetcher: pulls every upstream feed (EIA, NOAA, USGS, own bitcoind/LND)
# into var/feeds.db. EIA_API_KEY comes from /etc/auragrid/env (systemd EnvironmentFile).
cd /home/ubuntu/AurAgrid || exit 1
exec /home/ubuntu/miniconda/envs/auragrid_env/bin/python -u fetcher.py
