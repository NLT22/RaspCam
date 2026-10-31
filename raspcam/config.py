"""Load and validate config.yaml."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CameraConfig:
    device: str
    input_format: str = "mjpeg"
    width: int = 1920
    height: int = 1080
    input_fps: int = 30


@dataclass(frozen=True)
class LiveConfig:
    width: int = 1280
    height: int = 720
    fps: int = 5
    quality: int = 5  # ffmpeg -q:v, 2 (best) .. 31 (worst)


@dataclass(frozen=True)
class WebConfig:
    password_hash: str
    host: str = "0.0.0.0"
    port: int = 8080
    username: str = "admin"
    secret_key: str = ""  # empty: random per start (sessions end on restart)


@dataclass(frozen=True)
class RecordingConfig:
    fps: int = 10
    crf: int = 28            # x264 quality: lower is better and bigger
    preset: str = "veryfast"
    segment_seconds: int = 60
    keyframe_seconds: int = 2


@dataclass(frozen=True)
class StorageConfig:
    path: str = "~/raspcam-data"
    quota_gb: float = 5.0    # decimal GB of segments to keep
    min_free_gb: float = 1.0


@dataclass(frozen=True)
class Config:
    camera: CameraConfig
    live: LiveConfig
    web: WebConfig
    recording: RecordingConfig
    storage: StorageConfig


def _section(cls, data, name):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError(f"[{name}] must be a mapping")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ConfigError(f"[{name}] unknown keys: {', '.join(sorted(unknown))}")
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError(f"[{name}] {e}") from None


def _positive_int(name: str, value) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigError(f"{name} must be a positive integer, got {value!r}")


def _number(name: str, value, *, minimum: float, inclusive: bool) -> None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and (
        value >= minimum if inclusive else value > minimum)
    if not ok:
        bound = f">= {minimum}" if inclusive else f"> {minimum}"
        raise ConfigError(f"{name} must be a number {bound}, got {value!r}")


def parse_config(data: dict) -> Config:
    """Build a Config from parsed YAML. Unknown top-level sections are ignored
    so configs written for later milestones still load."""
    if not isinstance(data, dict):
        raise ConfigError("config must be a mapping")
    cfg = Config(
        camera=_section(CameraConfig, data.get("camera"), "camera"),
        live=_section(LiveConfig, data.get("live"), "live"),
        web=_section(WebConfig, data.get("web"), "web"),
        recording=_section(RecordingConfig, data.get("recording"), "recording"),
        storage=_section(StorageConfig, data.get("storage"), "storage"),
    )
    for name in ("width", "height", "input_fps"):
        _positive_int(f"camera.{name}", getattr(cfg.camera, name))
    for name in ("width", "height", "fps"):
        _positive_int(f"live.{name}", getattr(cfg.live, name))
    _positive_int("web.port", cfg.web.port)
    if not isinstance(cfg.live.quality, int) or not 2 <= cfg.live.quality <= 31:
        raise ConfigError(f"live.quality must be 2..31, got {cfg.live.quality!r}")
    for name in ("fps", "segment_seconds", "keyframe_seconds"):
        _positive_int(f"recording.{name}", getattr(cfg.recording, name))
    if not isinstance(cfg.recording.crf, int) or not 0 <= cfg.recording.crf <= 51:
        raise ConfigError(f"recording.crf must be 0..51, got {cfg.recording.crf!r}")
    if cfg.recording.keyframe_seconds > cfg.recording.segment_seconds:
        raise ConfigError("recording.keyframe_seconds must not exceed recording.segment_seconds")
    _number("storage.quota_gb", cfg.storage.quota_gb, minimum=0, inclusive=False)
    _number("storage.min_free_gb", cfg.storage.min_free_gb, minimum=0, inclusive=True)
    if not cfg.web.password_hash:
        raise ConfigError("web.password_hash is empty; generate one with: python -m raspcam.auth")
    return cfg


def load_config(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        return parse_config(yaml.safe_load(f))
