import pytest

from raspcam.config import ConfigError, load_config, parse_config

MINIMAL = {"camera": {"device": "testsrc"}, "web": {"password_hash": "scrypt$x"}}


def test_minimal_config_uses_defaults():
    cfg = parse_config(MINIMAL)
    assert cfg.camera.device == "testsrc"
    assert (cfg.camera.width, cfg.camera.height, cfg.camera.input_fps) == (1920, 1080, 30)
    assert (cfg.live.width, cfg.live.height, cfg.live.fps, cfg.live.quality) == (1280, 720, 5, 5)
    assert (cfg.web.host, cfg.web.port, cfg.web.username) == ("0.0.0.0", 8080, "admin")


def test_missing_camera_device_is_an_error():
    with pytest.raises(ConfigError, match="camera"):
        parse_config({"web": {"password_hash": "scrypt$x"}})


def test_unknown_key_in_section_is_an_error():
    data = {**MINIMAL, "live": {"fsp": 5}}
    with pytest.raises(ConfigError, match="fsp"):
        parse_config(data)


def test_empty_password_hash_is_an_error():
    data = {**MINIMAL, "web": {"password_hash": ""}}
    with pytest.raises(ConfigError, match="password_hash"):
        parse_config(data)


@pytest.mark.parametrize("section,key,value", [
    ("live", "fps", 0),
    ("live", "width", -1),
    ("camera", "input_fps", "30"),
    ("live", "quality", 40),
])
def test_invalid_numbers_are_errors(section, key, value):
    data = {**MINIMAL, section: {**MINIMAL.get(section, {}), key: value}}
    with pytest.raises(ConfigError, match=key):
        parse_config(data)


def test_unknown_top_level_sections_are_ignored():
    cfg = parse_config({**MINIMAL, "motion": {"fps": 5}})
    assert cfg.camera.device == "testsrc"


def test_load_config_reads_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("camera:\n  device: testsrc\nweb:\n  password_hash: 'scrypt$x'\n  port: 9000\n")
    assert load_config(path).web.port == 9000


def test_example_config_parses():
    from pathlib import Path
    example = Path(__file__).parent.parent / "config.example.yaml"
    text = example.read_text().replace('password_hash: ""', 'password_hash: "scrypt$x"')
    import yaml
    cfg = parse_config(yaml.safe_load(text))
    assert cfg.camera.device.startswith("/dev/v4l/by-id/")


def test_recording_and_storage_defaults():
    cfg = parse_config(MINIMAL)
    r, s = cfg.recording, cfg.storage
    assert (r.fps, r.crf, r.preset, r.segment_seconds, r.keyframe_seconds) == (10, 28, "veryfast", 60, 2)
    assert (s.path, s.quota_gb, s.min_free_gb) == ("~/raspcam-data", 5.0, 1.0)


@pytest.mark.parametrize("section,key,value", [
    ("recording", "fps", 0),
    ("recording", "segment_seconds", -5),
    ("recording", "crf", 60),
    ("storage", "quota_gb", 0),
    ("storage", "min_free_gb", -1),
])
def test_invalid_recording_and_storage_values(section, key, value):
    with pytest.raises(ConfigError, match=key):
        parse_config({**MINIMAL, section: {key: value}})


def test_keyframe_interval_must_fit_in_segment():
    with pytest.raises(ConfigError, match="keyframe_seconds"):
        parse_config({**MINIMAL, "recording": {"segment_seconds": 1, "keyframe_seconds": 2}})


def test_float_quota_is_accepted():
    assert parse_config({**MINIMAL, "storage": {"quota_gb": 0.5}}).storage.quota_gb == 0.5
