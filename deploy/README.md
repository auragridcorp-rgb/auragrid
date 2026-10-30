# Deployment

systemd units for the L402 stack, copied from `/etc/systemd/system/` on the
production server. Start order: pricer, backend, then Aperture.

| Unit | Runs | Listens on |
|---|---|---|
| `auragrid-pricer` | `scripts/run_pricer.sh` | 127.0.0.1:10010 (gRPC for Aperture dynamic pricing) |
| `auragrid-api` | `scripts/run_backend.sh` | 127.0.0.1:8001 (FastAPI, never public) |
| `aperture` | `scripts/run_aperture.sh` | 0.0.0.0:8000 (public L402 gateway) |

Secrets are not in these files. The launchers read them at start:

- `gateway.secret`: shared secret Aperture injects on paid routes (chmod 600)
- `feeds.env`: feed credentials, see `feeds.env.example` (chmod 600)
- `~/.bashrc`: `AURAGRID_*` settings (admin login, public URL, signing pubkey)
- `~/.aperture/aperture.yaml`: written by
  `python gen_aperture.py --out ~/.aperture/aperture.yaml --listenaddr 0.0.0.0:8000 --backend 127.0.0.1:8001 --gateway-secret-file gateway.secret`

Install or update:

    sudo cp deploy/systemd/*.service /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable --now auragrid-pricer auragrid-api aperture

Check from outside the server:

    python verify_deployment.py --base http://<public-ip>:8000 --paid-path /api/v1/batch/ercot-lmp \
        --ip <public-ip> --aperture-port 8000 --backend-port 8001

`auragrid-backend.service` (an older duplicate of `auragrid-api`) is disabled
on the server and intentionally not included here.
