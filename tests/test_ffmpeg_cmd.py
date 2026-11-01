from pathlib import Path

from raspcam.config import parse_config
from raspcam.ffmpeg_cmd import build_command

SEG = Path("/data/segments")


def make_cfg(device, **sections):
    return parse_config({"camera": {"device": device}, "web": {"password_hash": "scrypt$x"}, **sections})


def output_args(argv):
    """Split argv after the -filter_complex graph into [recording, live] output args."""
    start = argv.index("-filter_complex") + 2
    rest = argv[start:]
    second_map = rest.index("-map", 1)
    return rest[:second_map], rest[second_map:]


def test_v4l2_input():
    argv = build_command(make_cfg("/dev/video0"), SEG)
    i = argv.index("-i")
    assert argv[i - 8:i + 2] == ["-f", "v4l2", "-input_format", "mjpeg", "-video_size", "1920x1080",
                                 "-framerate", "30", "-i", "/dev/video0"]
    assert "-nostdin" in argv and argv[argv.index("-loglevel") + 1] == "error"


def test_test_source_uses_lavfi_in_real_time():
    argv = build_command(make_cfg("testsrc"), SEG)
    i = argv.index("-i")
    assert argv[i - 3:i + 2] == ["-re", "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30"]


def test_filter_graph_splits_recording_and_live():
    argv = build_command(make_cfg("/dev/video0"), SEG)
    assert argv[argv.index("-filter_complex") + 1] == (
        "[0:v]split=2[r][l];[r]fps=10[rec];[l]fps=5,scale=1280:720[live]")


def test_recording_output_is_h264_segments():
    rec, _ = output_args(build_command(make_cfg("/dev/video0"), SEG))
    assert rec[:2] == ["-map", "[rec]"]
    opts = dict(zip(rec[2:-1:2], rec[3:-1:2]))
    assert opts["-c:v"] == "libx264" and opts["-preset"] == "veryfast" and opts["-crf"] == "28"
    assert opts["-g"] == "20" and opts["-pix_fmt"] == "yuv420p"
    assert opts["-f"] == "segment" and opts["-segment_time"] == "60"
    assert opts["-segment_atclocktime"] == "1" and opts["-strftime"] == "1"
    assert opts["-reset_timestamps"] == "1"
    assert opts["-segment_format_options"] == "movflags=+faststart"
    assert rec[-1] == "/data/segments/%Y%m%d-%H%M%S.mp4"


def test_live_output_is_mjpeg_on_stdout():
    _, live = output_args(build_command(make_cfg("/dev/video0"), SEG))
    assert live == ["-map", "[live]", "-c:v", "mjpeg", "-q:v", "5", "-f", "mjpeg", "pipe:1"]


def test_recording_options_follow_config():
    cfg = make_cfg("/dev/video0", recording={"fps": 5, "crf": 30, "preset": "ultrafast",
                                             "segment_seconds": 30, "keyframe_seconds": 3})
    rec, _ = output_args(build_command(cfg, SEG))
    opts = dict(zip(rec[2:-1:2], rec[3:-1:2]))
    assert (opts["-crf"], opts["-preset"], opts["-g"], opts["-segment_time"]) == ("30", "ultrafast", "15", "30")


def test_motion_output_on_extra_fd():
    argv = build_command(make_cfg("/dev/video0"), SEG, motion_fd=7)
    assert argv[argv.index("-filter_complex") + 1] == (
        "[0:v]split=3[r][l][m];[r]fps=10[rec];[l]fps=5,scale=1280:720[live];"
        "[m]fps=5,scale=320:180,format=gray[motion]")
    assert argv[-7:] == ["-map", "[motion]", "-f", "rawvideo", "-pix_fmt", "gray", "pipe:7"]


def test_no_motion_output_without_fd():
    argv = build_command(make_cfg("/dev/video0"), SEG)
    assert "[motion]" not in " ".join(argv) and argv[-1] == "pipe:1"
