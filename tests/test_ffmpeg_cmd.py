from raspcam.config import parse_config
from raspcam.ffmpeg_cmd import build_command


def make_cfg(device):
    return parse_config({"camera": {"device": device}, "web": {"password_hash": "scrypt$x"}})


def test_v4l2_camera_command():
    argv = build_command(make_cfg("/dev/video0"))
    assert argv[0] == "ffmpeg"
    i = argv.index("-i")
    assert argv[i + 1] == "/dev/video0"
    assert argv[i - 8:i] == ["-f", "v4l2", "-input_format", "mjpeg",
                             "-video_size", "1920x1080", "-framerate", "30"]
    assert argv[argv.index("-vf") + 1] == "fps=5,scale=1280:720"
    assert argv[argv.index("-c:v"):] == ["-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "pipe:1"]
    assert "-nostdin" in argv


def test_test_source_uses_lavfi_in_real_time():
    argv = build_command(make_cfg("testsrc"))
    i = argv.index("-i")
    assert argv[i + 1] == "testsrc2=size=1920x1080:rate=30"
    assert argv[i - 2:i] == ["-f", "lavfi"]
    assert "-re" in argv[:i]
    assert "v4l2" not in argv
