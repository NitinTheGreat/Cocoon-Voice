"""Generate the ORIGINAL captioned demo clip for lesson L1 (seatbelt) with ffmpeg, plus its WebVTT captions.

    python scripts/make_lesson_video.py            # writes content/media/L1_seatbelt_demo_v1.mp4 and .vtt
    python scripts/make_lesson_video.py --verify   # decodes the committed file with ffprobe/ffmpeg and prints facts

Provenance: every frame is generated here from plain colour cards and the caption text below (no third-party
footage, images or audio). It is a demo teaching aid written for this prototype and has NOT been reviewed by a trainer
or by Caterpillar. The output is a silent H.264 MP4 (burned-in captions plus a separate .vtt track) so it
plays in a mobile video player and in browsers. Requires ffmpeg with the drawtext filter (libfreetype).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
OUT = SERVICE_DIR / "content" / "media" / "L1_seatbelt_demo_v1.mp4"
VTT = OUT.with_suffix(".vtt")
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SECONDS_PER_CARD = 4
CARDS = [  # (background colour, caption line 1, caption line 2) — original demo text
    ("0x1f3b4d", "Seatbelt basics", "demo lesson, about 60 seconds"),
    ("0x2e5e3a", "1. Fasten the seatbelt", "before you start the engine"),
    ("0x2e5e3a", "2. Keep it fastened while the engine runs", "even when you are waiting"),
    ("0x7a4a12", "3. Never unbuckle to lean out", "while the machine can move"),
    ("0x7a2020", "4. If the machine tips:", "stay in the seat, hold on, brace"),
    ("0x1f3b4d", "5. Report a damaged belt", "before you operate"),
]


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace(",", "\\,")


def build(ffmpeg: str) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    inputs, filters = [], []
    for i, (colour, line1, line2) in enumerate(CARDS):
        inputs += ["-f", "lavfi", "-t", str(SECONDS_PER_CARD), "-i", f"color=c={colour}:s=640x360:r=15"]
        filters.append(
            f"[{i}:v]drawtext=fontfile={FONT}:text='{_escape(line1)}':fontcolor=white:fontsize=26:"
            "x=(w-text_w)/2:y=h/2-44:box=1:boxcolor=black@0.35:boxborderw=10,"
            f"drawtext=fontfile={FONT}:text='{_escape(line2)}':fontcolor=white:fontsize=22:"
            "x=(w-text_w)/2:y=h/2+8:box=1:boxcolor=black@0.35:boxborderw=10,"
            f"drawtext=fontfile={FONT}:text='Cocoon demo clip - not reviewed by a trainer':fontcolor=white@0.8:"
            f"fontsize=13:x=12:y=h-28[v{i}]")
    concat = "".join(f"[v{i}]" for i in range(len(CARDS))) + f"concat=n={len(CARDS)}:v=1:a=0[out]"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", ";".join(filters) + ";" + concat, "-map", "[out]",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "baseline", "-level", "3.0", "-crf", "30",
           "-movflags", "+faststart", "-fflags", "+bitexact", "-flags:v", "+bitexact",
           "-metadata", "title=Cocoon L1 seatbelt demo clip (original, unreviewed)", str(OUT)]
    subprocess.run(cmd, check=True)
    lines = ["WEBVTT", ""]
    for i, (_, line1, line2) in enumerate(CARDS):
        start, end = i * SECONDS_PER_CARD, (i + 1) * SECONDS_PER_CARD
        lines += [f"00:00:{start:02d}.000 --> 00:00:{end:02d}.000", line1, line2, ""]
    VTT.write_text("\n".join(lines), encoding="utf-8")


def verify(ffprobe: str, ffmpeg: str) -> dict:
    probe = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration,format_name:stream=codec_name,width,height",
         "-of", "json", str(OUT)], check=True, capture_output=True, text=True).stdout)
    # full decode of every frame: fails on a truncated or corrupt file
    subprocess.run([ffmpeg, "-v", "error", "-i", str(OUT), "-f", "null", "-"], check=True)
    stream = probe["streams"][0]
    return {"path": str(OUT.relative_to(SERVICE_DIR)), "bytes": OUT.stat().st_size,
            "sha256": hashlib.sha256(OUT.read_bytes()).hexdigest(), "codec": stream["codec_name"],
            "width": stream["width"], "height": stream["height"],
            "duration_seconds": round(float(probe["format"]["duration"]), 2), "decoded": True}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        print("ffmpeg/ffprobe not found", file=sys.stderr)
        return 1
    if not args.verify:
        build(ffmpeg)
    print(json.dumps(verify(ffprobe, ffmpeg), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
