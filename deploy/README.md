# Deployment

systemd units for the L402 stack, copied from `/etc/systemd/system/` on the
production server. Start order: pricer, backend, then Aperture.

| Unit | Runs | Listens on |
|---|---|---|
| `auragrid-pricer` | `scripts/run_pricer.sh` | 127.0.0.1:10010 (gRPC for Aperture dynamic pricing) |
| `auragrid-api` | `scripts/run_backend.sh` | 127.0.0.1:8000 (FastAPI, never public) |
| `aperture` | `scripts/run_aperture.sh` | 0.0.0.0:8080 (public L402 gateway; firewall + OCI security list must allow TCP 8080) |

Secrets are not in these files. The launchers read them at start:

- `gateway.secret`: shared secret Aperture injects on paid routes (chmod 600)
- `feeds.env`: feed credentials, see `feeds.env.example` (chmod 600)
- `~/.bashrc`: `AURAGRID_*` settings (admin user, public URL, signing pubkey)
- `~/.auragrid/admin_password`: dashboard password (chmod 600)
- `~/.aperture/aperture.yaml`: written by
  `python gen_aperture.py --out ~/.aperture/aperture.yaml --listenaddr 0.0.0.0:8080 --backend 127.0.0.1:8000 --gateway-secret-file gateway.secret`

Install or update:

    sudo cp deploy/systemd/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now auragrid-pricer auragrid-api aperture

Check from outside the server:

    python verify_deployment.py --base http://<public-ip>:8080 --ip <public-ip>

`auragrid-backend.service` (an older duplicate of `auragrid-api`) is disabled
on the server and intentionally not included here.
