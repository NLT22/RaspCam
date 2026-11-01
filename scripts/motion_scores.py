"""Print motion scores for recorded segments, to tune the motion settings.

Usage (on the Pi):  .venv/bin/python scripts/motion_scores.py ~/raspcam-data/segments/2026*.mp4
"""
import argparse
import subprocess

from raspcam.config import load_config
from raspcam.motion import MotionDetector


def scores(path, m):
    proc = subprocess.Popen(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
         "-vf", f"fps={m.fps},scale={m.width}:{m.height},format=gray",
         "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1"], stdout=subprocess.PIPE)
    detector = MotionDetector(m.width, m.height, m.pixel_threshold)
    size = m.width * m.height
    while len(frame := proc.stdout.read(size)) == size:
        yield detector.score(frame)
    proc.wait()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("files", nargs="+")
    args = ap.parse_args()
    m = load_config(args.config).motion
    for f in args.files:
        s = sorted(scores(f, m))
        if not s:
            print(f"{f}: no frames")
            continue
        pct = lambda q: s[min(len(s) - 1, int(q * len(s)))] * 100
        moving = sum(v * 100 >= m.area_percent for v in s)
        print(f"{f}: median {pct(.5):.2f}%  p95 {pct(.95):.2f}%  max {s[-1] * 100:.2f}%  "
              f"frames over area_percent: {moving}/{len(s)}")


if __name__ == "__main__":
    main()
