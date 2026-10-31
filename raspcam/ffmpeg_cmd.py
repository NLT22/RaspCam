"""Build the ffmpeg command line from config."""
from __future__ import annotations

from .config import Config

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


def build_command(cfg: Config) -> list[str]:
    """ffmpeg argv that writes the live MJPEG stream to stdout."""
    live = cfg.live
    return [
        # "error": swscale prints a harmless "deprecated pixel format" warning for
        # MJPEG input that would otherwise show up as last_error in the UI
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        *_input_args(cfg),
        "-vf", f"fps={live.fps},scale={live.width}:{live.height}",
        "-c:v", "mjpeg", "-q:v", str(live.quality),
        "-f", "mjpeg", "pipe:1",
    ]
