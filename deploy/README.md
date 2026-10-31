# Deployment

systemd units for the L402 stack, copied from `/etc/systemd/system/` on the
production server. Start order: pricer, backend, then Aperture.

| Unit | Runs | Listens on |
|---|---|---|
| `auragrid-pricer` | `scripts/run_pricer.sh` | 127.0.0.1:10010 (gRPC for Aperture dynamic pricing) |
| `auragrid-api` | `scripts/run_backend.sh` | 127.0.0.1:8000 (FastAPI, never public) |
| `auragrid-fetcher` | `scripts/run_fetcher.sh` | nothing (writes `var/feeds.db`; reads `EIA_API_KEY` from `/etc/auragrid/env`; sandboxed read-only except `var/`) |
| `aperture` | `scripts/run_aperture.sh` | 0.0.0.0:443 (public L402 gateway, HTTPS; binds :443 via `CAP_NET_BIND_SERVICE`) |
| `auragrid-cert-renew` (+ `.timer`) | `lego` (twice daily) | :80 only while renewing |

Secrets are not in these files. The launchers read them at start:

- `gateway.secret`: shared secret Aperture injects on paid routes (chmod 600)
- `feeds.env`: feed credentials, see `feeds.env.example` (chmod 600)
- `~/.bashrc`: `AURAGRID_*` settings (admin user, public URL, signing pubkey)
- `~/.auragrid/admin_password`: dashboard password (chmod 600)
- `~/.aperture/aperture.yaml`: written by
  `python gen_aperture.py --out ~/.aperture/aperture.yaml --listenaddr 0.0.0.0:443 --tls --backend 127.0.0.1:8000 --gateway-secret-file gateway.secret`

Install or update:

    sudo cp deploy/systemd/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now auragrid-pricer auragrid-fetcher auragrid-api aperture

Check from outside the server:

    python verify_deployment.py --base https://84.12.79.124 --ip 84.12.79.124

`auragrid-backend.service` (an older duplicate of `auragrid-api`) is disabled
on the server and intentionally not included here.

## HTTPS (free, no domain)

Aperture terminates TLS itself (a proxy in front would hide client IPs from its
per-IP free-tier rate limit) with a Let's Encrypt certificate for the bare IP:
the `shortlived` profile (about 6 days), renewed at about half its lifetime.

- Firewall and OCI security list: TCP 443 (service) and TCP 80 (HTTP-01 renewals).
- `lego` in `/usr/local/bin`, state in `/etc/lego` (root).
- Install once:

      sudo install -o root -g root -m 755 scripts/install_tls_cert.sh /usr/local/sbin/auragrid-install-tls-cert
      sudo cp deploy/systemd/auragrid-cert-renew.* /etc/systemd/system/
      sudo systemctl daemon-reload && sudo systemctl enable --now auragrid-cert-renew.timer

- Check: `systemctl list-timers auragrid-cert-renew.timer`,
  `openssl x509 -in ~/.aperture/tls.cert -noout -dates`.
