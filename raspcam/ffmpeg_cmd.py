"""Build the ffmpeg command line from config."""
from __future__ import annotations

from pathlib import Path

from .config import Config
from .segments import SEGMENT_PATTERN

TEST_SOURCE = "testsrc"


def _input_args(cfg: Config) -> list[str]:
    cam = cfg.camera
    if cam.device == TEST_SOURCE:
        # -re: lavfi would otherwise generate frames as fast as the CPU allows
        return ["-re", "-f", "lavfi", "-i",
                f"testsrc2=size={cam.width}x{cam.height}:rate={cam.input_fps}"]
    return ["-f", "v4l2", "-input_format", cam.input_format,
            "-video_size", f"{cam.width}x{cam.height}",
            "-framerate", str(cam.input_fps), "-i", cam.device]


def build_command(cfg: Config, segments_dir: Path, motion_fd: int | None = None) -> list[str]:
    """ffmpeg argv: H.264 segments into segments_dir, live MJPEG on stdout, and
    (if motion_fd is given) small gray frames for motion detection on that fd."""
    rec, live, mo = cfg.recording, cfg.live, cfg.motion
    branches = ["[r]", "[l]"] + (["[m]"] if motion_fd is not None else [])
    graph = [f"[0:v]split={len(branches)}{''.join(branches)}",
             f"[r]fps={rec.fps}[rec]",
             f"[l]fps={live.fps},scale={live.width}:{live.height}[live]"]
    if motion_fd is not None:
        graph.append(f"[m]fps={mo.fps},scale={mo.width}:{mo.height},format=gray[motion]")
    argv = [
        # "error": swscale prints a harmless "deprecated pixel format" warning for
        # MJPEG input that would otherwise show up as last_error in the UI
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        *_input_args(cfg),
        "-filter_complex", ";".join(graph),
        "-map", "[rec]",
        "-c:v", "libx264", "-preset", rec.preset, "-crf", str(rec.crf),
        "-pix_fmt", "yuv420p", "-g", str(rec.fps * rec.keyframe_seconds),
        "-f", "segment", "-segment_time", str(rec.segment_seconds),
        "-segment_atclocktime", "1", "-reset_timestamps", "1", "-strftime", "1",
        "-segment_format_options", "movflags=+faststart",
        str(segments_dir / SEGMENT_PATTERN),
        "-map", "[live]", "-c:v", "mjpeg", "-q:v", str(live.quality),
        "-f", "mjpeg", "pipe:1",
    ]
    if motion_fd is not None:
        argv += ["-map", "[motion]", "-f", "rawvideo", "-pix_fmt", "gray", f"pipe:{motion_fd}"]
    return argv
