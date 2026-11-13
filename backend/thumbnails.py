"""Clip thumbnail generation from a real rendered frame."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from backend.ingestion import IngestionError


def create_thumbnail(video_path: Path, output_path: Path, text: str, timestamp: float = 1.0) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_frame = output_path.with_suffix(".frame.jpg")
    command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-ss", f"{max(0.0, timestamp):.3f}",
        "-i", str(video_path), "-frames:v", "1", "-q:v", "2", str(raw_frame),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    if result.returncode or not raw_frame.is_file():
        raw_frame.unlink(missing_ok=True)
        raise IngestionError("FFmpeg could not extract a thumbnail frame.")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        raw_frame.replace(output_path)
        return output_path
    try:
        image = Image.open(raw_frame).convert("RGB")
        target_w, target_h = 1080, 1920
        scale = max(target_w / image.width, target_h / image.height)
        resized = image.resize((round(image.width * scale), round(image.height * scale)))
        left = max(0, (resized.width - target_w) // 2)
        top = max(0, (resized.height - target_h) // 2)
        image = resized.crop((left, top, left + target_w, top + target_h))
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(overlay)
        for index in range(760):
            alpha = int(210 * (index / 760) ** 1.25)
            draw.rectangle([(0, 1160 + index), (target_w, 1161 + index)], fill=(5, 4, 8, alpha))
        font_path = Path("C:/Windows/Fonts/arialbd.ttf")
        font = ImageFont.truetype(str(font_path), 88) if font_path.exists() else ImageFont.load_default()
        wrapped = textwrap.wrap(text.strip().upper() or "PODCAST CLIP", width=17)[:4]
        line_height = 104
        start_y = 1650 - line_height * len(wrapped)
        for index, line in enumerate(wrapped):
            draw.text((70, start_y + index * line_height), line, font=font, fill=(255, 255, 255, 255),
                      stroke_width=5, stroke_fill=(10, 8, 14, 230))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
        image.save(output_path, "JPEG", quality=90)
        return output_path
    finally:
        raw_frame.unlink(missing_ok=True)
