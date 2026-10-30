#!/bin/bash
# Dynamic pricing gRPC for Aperture (127.0.0.1:10010).
cd /home/ubuntu/AurAgrid || exit 1
exec /home/ubuntu/miniconda/envs/auragrid_env/bin/python -u pricer.py
