"""Render force_style values and measure glyph bounds; no personal media needed."""
from pathlib import Path
import json
import subprocess
import tempfile


def probe():
    results = {}
    width, height = 640, 360
    with tempfile.TemporaryDirectory(prefix="bili2x-alignment-") as directory:
        (Path(directory) / "captions.srt").write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nTEST\n", encoding="utf-8")
        for alignment in (6, 8):
            style = f"Alignment={alignment},FontSize=36,Outline=0,Shadow=0,MarginV=10"
            command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                       f"color=c=black:s={width}x{height}:d=1", "-vf",
                       f"subtitles=filename=captions.srt:force_style='{style}'",
                       "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"]
            raw = subprocess.run(command, cwd=directory, capture_output=True, check=True).stdout
            if len(raw) != width * height * 3:
                raise RuntimeError("Unexpected raw frame size")
            points = [(i % width, i // width) for i in range(width * height)
                      if max(raw[i * 3:i * 3 + 3]) > 100]
            if not points:
                raise RuntimeError("No visible glyphs; check fonts and subtitle filter support")
            results[str(alignment)] = {
                "left": min(x for x, y in points), "right": max(x for x, y in points),
                "top": min(y for x, y in points), "bottom": max(y for x, y in points),
            }
    print(json.dumps(results, indent=2))
    return results


if __name__ == "__main__":
    probe()
