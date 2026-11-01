# RaspCam

Security camera for a Raspberry Pi 5 with a USB webcam: live view in the
browser, H.264 recording with timeline playback, motion detection with
events, and a storage quota (milestones 1–3). Design:
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

## Motion detection

`recording.mode` chooses what is kept:

- `both` (default): record everything and mark motion events on the timeline.
- `motion`: keep only the minutes around motion events (`motion.padding_segments`).
- `continuous`: record everything, no motion detection.

Too many false alarms? Raise `motion.area_percent` (or `pixel_threshold`). To see
what values your camera produces, score some recordings:

```bash
.venv/bin/python scripts/motion_scores.py ~/raspcam-data/segments/20261101-09*.mp4
```

## Deploy to the Pi

```bash
scripts/deploy.sh        # rsync to nlt22@192.168.1.90:~/raspcam, install, restart service
```

The first run stops at "config.yaml missing": create it on the Pi in
`~/raspcam` as above, then run the script again.

Open http://192.168.1.90:8080

Logs: `ssh nlt22@192.168.1.90 journalctl -u raspcam -f`
