# RaspCam

Security camera for a Raspberry Pi 5 with a USB webcam: live view in the
browser, continuous H.264 recording with playback, and a storage quota
(milestones 1–2). Motion detection follows —
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

Recordings go to `storage.path` (default `~/raspcam-data`): one MP4 per
minute in `segments/`, indexed in `raspcam.db`. The oldest segments are deleted
when they exceed `storage.quota_gb` or the disk has less than
`storage.min_free_gb` free. Play them back on the "Xem lại" page (`/recordings`):
a day timeline, an hour view with 5-minute thumbnails (`thumbs/`), playback
speed up to 16x, keyboard shortcuts, and a download button per segment.

## Deploy to the Pi

```bash
scripts/deploy.sh        # rsync to nlt22@192.168.1.90:~/raspcam, install, restart service
```

The first run stops at "config.yaml missing": create it on the Pi in
`~/raspcam` as above, then run the script again.

Open http://192.168.1.90:8080

Logs: `ssh nlt22@192.168.1.90 journalctl -u raspcam -f`
