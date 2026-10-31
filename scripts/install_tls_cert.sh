#!/bin/bash
# Installed root-owned as /usr/local/sbin/auragrid-install-tls-cert (see deploy/README.md).
# lego deploy hook (runs as root after a certificate is issued or renewed):
# install the Let's Encrypt IP certificate where Aperture reads it, then restart
# Aperture, which only loads its certificate at startup.
set -euo pipefail
SRC=/etc/lego/certificates/84.12.79.124
DST=/home/ubuntu/.aperture
install -o ubuntu -g ubuntu -m 600 "$SRC.crt" "$DST/tls.cert.new"
install -o ubuntu -g ubuntu -m 600 "$SRC.key" "$DST/tls.key.new"
mv -f "$DST/tls.cert.new" "$DST/tls.cert"
mv -f "$DST/tls.key.new" "$DST/tls.key"
systemctl try-restart aperture.service
