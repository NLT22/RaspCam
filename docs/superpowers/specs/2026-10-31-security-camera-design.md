# RaspCam — Security Camera on Raspberry Pi 5 — Design

Date: 2026-10-31
Status: Approved (brainstorming), pending implementation plan

## 1. Goal

A self-hosted security camera running on a Raspberry Pi 5 with one USB webcam.
It records video to local storage, detects motion, and offers a web UI on the
LAN for live view, motion events, and playback. Remote (off-LAN) access is a
later, optional addition; the design must not block it.

### In scope (v1)

- Recording modes: `continuous`, `motion`, `both` (selectable in config).
- Motion detection with event list and thumbnails.
- Web UI on LAN: live view, events, playback by day, status page.
- Password-protected web UI.
- Automatic retention: storage quota and minimum free space.
- Runs as a systemd service; survives camera unplug, ffmpeg crashes, power loss.

### Out of scope (v1)

- Remote access (Tailscale) — planned for v2; v1 only requires auth so it can
  be exposed safely later.
- Notifications (Telegram etc.) — v2+.
- Audio recording — webcam has a mic, can be added later.
- Multiple cameras, object detection (people/vehicles), motion zones.

## 2. Hardware and environment

| Item | Detail |
|---|---|
| Board | Raspberry Pi 5, 4 cores, 8 GB RAM, 5 A PSU (`usb_max_current_enable=1`) |
| OS | Debian 13 (trixie), aarch64, kernel 6.18 rpi-2712 |
| Host | `nlt22@192.168.1.90`, SSH key auth |
| Camera | USB "Wed Camera" `32e6:9221`, `/dev/video0`; MJPEG 1920x1080@30, 1280x720@30, …; YUYV only 5 fps at 1080p |
| Storage | microSD 29 GB (17 GB free) for development. Planned external 2.5" SATA disk via USB enclosure; the original 128 GB HDD is dead (not detected by the enclosure on Pi or PC, not detected on direct SATA, does not spin). |
| Tools on Pi | `ffmpeg`, Python 3.13.5, `v4l2-ctl`, passwordless sudo |

Constraint: the Pi 5 has **no hardware H.264 encoder**, so encoding is software
`libx264`. 1080p@10fps with a fast preset fits comfortably in 4 cores.

Constraint: a V4L2 device can be opened by **one process only**, so a single
ffmpeg process reads the camera and produces all outputs.

### Storage estimates (H.264, typical indoor scene)

| Config | Bitrate | GB/day | Days on 128 GB |
|---|---|---|---|
| 1080p @ 30 fps | ~4 Mbps | ~43 | ~3 |
| 1080p @ 10 fps | ~1.5 Mbps | ~16 | ~8 |
| 1080p @ 5 fps | ~1 Mbps | ~11 | ~11 |
| 720p @ 10 fps | ~0.8 Mbps | ~9 | ~14 |

## 3. Architecture

One Python application (single systemd service) that supervises one ffmpeg
child process.

```
 Webcam (MJPEG 1080p30)
        │
        ▼
  ┌────────────── ffmpeg (supervised by app) ──────────────┐
  │ ① H.264 1080p@10fps → 60 s .mp4 segments → storage     │
  │ ② gray 320x180 @5fps rawvideo → pipe → motion          │
  │ ③ MJPEG 720p @5fps → pipe → live view                  │
  └────────────────────────────────────────────────────────┘
        │                     │
        ▼                     ▼
   retention  ◄──── SQLite (segments + motion events)
                             ▲
                             │
             web (FastAPI): live · events · playback · status
```

### Modules

Each module has one purpose and a narrow interface.

| Module | Responsibility | Depends on |
|---|---|---|
| `config` | Load and validate `config.yaml` into a typed object | — |
| `db` | SQLite schema and queries for segments and events | — |
| `ffmpeg_cmd` | Pure function: config → ffmpeg argv | `config` |
| `recorder` | Start/supervise ffmpeg, restart with backoff, read the segment list and register finished segments in `db`, drain output pipes | `ffmpeg_cmd`, `db` |
| `motion` | Pure detector (frame in → motion score / state); event tracker that opens/closes events in `db` and saves thumbnails | `db` |
| `retention` | Pure planner (segments, events, mode, quota, free space → files to delete) plus a periodic job that applies it | `db` |
| `reconcile` | On startup, make `db` match files on disk | `db` |
| `web` | FastAPI app: auth, live MJPEG, events, playback, status | `db`, live frame buffer |
| `main` | Wire everything together, run until signalled | all |

Pure parts (`ffmpeg_cmd`, motion detector, retention planner, reconcile logic)
contain the decisions and are unit tested without ffmpeg, a camera, or a disk.

## 4. Configuration (`config.yaml`)

```yaml
camera:
  device: /dev/v4l/by-id/usb-Wed_Camera_Wed_Camera_20240420112206-video-index0
  input_format: mjpeg
  width: 1920
  height: 1080
  input_fps: 30

recording:
  mode: both            # continuous | motion | both
  fps: 10
  crf: 28
  preset: veryfast
  segment_seconds: 60

motion:
  width: 320
  height: 180
  fps: 5
  pixel_threshold: 25     # per-pixel diff (0-255) counted as changed
  area_percent: 1.0       # % of changed pixels to count a frame as motion
  min_frames: 2           # consecutive motion frames to start an event
  cooldown_seconds: 10    # no motion for this long ends the event
  padding_segments: 1     # segments kept before/after an event in motion mode

live:
  width: 1280
  height: 720
  fps: 5

storage:
  path: /home/nlt22/raspcam-data
  quota_gb: 5             # dev on microSD; raise when the external disk exists
  min_free_gb: 1
  require_mount: false    # true once storage is on an external disk

web:
  host: 0.0.0.0
  port: 8080
  username: admin
  password_hash: "..."    # generated by a CLI helper, never plain text
```

The `/dev/v4l/by-id/...` path above was verified on the Pi (index0 is the
capture node; index1 is metadata).

## 5. Data flow

### Recording

- ffmpeg reads MJPEG 1080p30 from the camera, decodes once, and splits into
  three outputs (`-filter_complex split`).
- Output ①: `fps=10`, `libx264` (`preset`, `crf` from config), segment muxer:
  `-f segment -segment_time 60 -segment_atclocktime 1 -reset_timestamps 1
  -strftime 1`, files `segments/%Y-%m-%d/%H-%M-%S.mp4`, with
  `-segment_format_options movflags=+faststart`.
  Date directories are created ahead of time by the recorder (today and
  tomorrow) because the segment muxer does not create directories.
- ffmpeg writes finished segment names to a segment list
  (`-segment_list … -segment_list_type csv`). The recorder tails that file and
  inserts a row per finished segment: `path, start_ts, end_ts, size_bytes`.
- Output ②: `fps=5,scale=320:180,format=gray`, rawvideo to a pipe.
- Output ③: `fps=5,scale=1280:720`, MJPEG to a pipe; the recorder splits JPEGs
  by SOI/EOI markers and keeps only the latest frame in an in-memory buffer.

### Motion detection

- Each gray frame: light blur (box filter), `absdiff` against a running-average
  background (`bg = (1-α)·bg + α·frame`, α ≈ 0.05), count pixels whose diff
  exceeds `pixel_threshold`.
- A frame is "motion" when changed pixels ≥ `area_percent` of the frame.
- An event starts after `min_frames` consecutive motion frames and ends after
  `cooldown_seconds` without motion. Events store `start_ts`, `end_ts`,
  `peak_score`, and a thumbnail JPEG (latest live frame at event start).
- The adaptive background absorbs slow lighting changes (dusk, clouds).
- Implemented with numpy only (no OpenCV dependency).

### Retention (runs every 60 s)

The planner is a pure function returning the paths to delete:

1. Mode `motion`: delete finished segments that do not overlap any event
   window `[event.start - padding, event.end + padding]` (padding =
   `padding_segments × segment_seconds`). Segments younger than one padding
   window are never deleted, so an event that starts soon after still gets its
   pre-roll. An event still open counts as ending "now".
2. Modes `continuous` and `both`: no mode-based deletion.
3. All modes: while total segment size > `quota_gb` or filesystem free space <
   `min_free_gb`, delete the oldest segment.

Events whose segments are all gone are deleted along with their thumbnails.

### Web

- Auth: username + password (hash in config), signed session cookie. All
  routes except the login page require auth.
- Live: `multipart/x-mixed-replace` MJPEG stream from the latest-frame buffer.
- Events: list newest first with thumbnail, time, duration; clicking opens the
  segment containing the event at the right offset (`#t=<seconds>`).
- Playback: pick a day → list of segments; the player auto-advances to the next
  segment. MP4 files are served with HTTP Range support.
- Status: camera online/offline, ffmpeg uptime and restart count, storage used
  vs quota, free space, CPU load, SoC temperature.

## 6. Error handling

| Situation | Behaviour |
|---|---|
| Camera unplugged / ffmpeg crashes | Restart with exponential backoff (1 s → 30 s cap). Status shows "Camera offline". Camera addressed by `/dev/v4l/by-id/...` so re-plugging works. |
| Power loss | At most the in-progress segment is lost. On startup `reconcile` indexes files missing from the DB, drops DB rows for missing files, and deletes 0-byte or unreadable (`ffprobe` fails) segments. |
| Slow motion/live consumer | Dedicated reader threads drain pipes continuously and drop frames when consumers lag, so ffmpeg never blocks and recording is never affected. |
| External disk not mounted | If `require_mount: true` and `storage.path` is not on a mounted filesystem other than `/`, the recorder does not start ffmpeg and status reports the error (prevents filling the microSD). |
| Disk nearly full | Retention enforces `min_free_gb`; status shows a warning. |
| High CPU / temperature | Shown on the status page and logged. |

## 7. Testing

- **Unit (pytest, on PC):** retention planner (modes, padding, quota, free
  space, open events), motion detector on synthetic numpy frames (static scene,
  moving block, slow brightness ramp), `ffmpeg_cmd` output, reconcile logic,
  config validation, auth.
- **Integration (on PC):** run the recorder with ffmpeg `lavfi testsrc` as a
  fake camera; verify segments appear and are registered, live frames arrive,
  and web endpoints work via FastAPI `TestClient`.
- **On Pi (manual smoke test):** run 10–15 min with the real webcam; check CPU
  and temperature, segment sizes vs the estimates, and that waving at the
  camera produces an event with a thumbnail and correct playback offset.

## 8. Project layout and deployment

```
raspcam/            Python package (modules above)
templates/, static/ Web UI
tests/
config.example.yaml
deploy/raspcam.service
scripts/deploy.sh   rsync to Pi + restart service
docs/superpowers/specs/
```

- Develop on the PC in this folder; `scripts/deploy.sh` rsyncs to the Pi
  (`~/raspcam`), installs deps into a venv (Python 3.13), and restarts the
  systemd service.
- Dependencies: `fastapi`, `uvicorn`, `numpy`, `pyyaml`, `jinja2`
  (+ `itsdangerous` for signed cookies); dev: `pytest`, `httpx`.
- Recordings live outside the code directory (`storage.path`).

## 9. Future (v2+)

- Remote access via Tailscale (no router port forwarding).
- Telegram notification with snapshot on motion.
- Audio recording, motion zones/masks, object detection.
