# RaspCam

Security camera for a Raspberry Pi 5 with a USB webcam: live view in the
browser (milestone 1). Recording, motion detection, and retention follow —
see `docs/superpowers/specs/2026-10-31-security-camera-design.md`.

## Development (PC)

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

Without a camera, set `camera.device: testsrc` to use an ffmpeg test pattern.

## Config

```bash
cp config.example.yaml config.yaml
.venv/bin/python -m raspcam.auth   # prints password_hash and secret_key for the web section
.venv/bin/python -m raspcam --config config.yaml
```

## Deploy to the Pi

```bash
scripts/deploy.sh        # rsync to nlt22@192.168.1.90:~/raspcam, install, restart service
```

The first run stops at "config.yaml missing": create it on the Pi in
`~/raspcam` as above, then run the script again.

Open http://192.168.1.90:8080

Logs: `ssh nlt22@192.168.1.90 journalctl -u raspcam -f`
