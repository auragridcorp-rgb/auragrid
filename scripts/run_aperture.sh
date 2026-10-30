#!/bin/bash
# Aperture with the config written by gen_aperture.py (contains the gateway secret).
exec /home/ubuntu/go/bin/aperture --configfile /home/ubuntu/.aperture/aperture.yaml
