from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1080, 1920


def _probe(path: Path) -> dict:
    result: dict = {}
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries",
                "stream=index,codec_type,codec_name,width,height:"
                "format=duration",
                "-of", "json",
                str(path),
            ],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(r.stdout)
        for stream in data.get("streams", []):
            ctype = stream.get("codec_type")
            if ctype == "video":
                result["width"] = stream.get("width")
                result["height"] = stream.get("height")
                result["video_codec"] = stream.get("codec_name")
                result["video_stream"] = stream.get("index")
            elif ctype == "audio":
                result["audio_codec"] = stream.get("codec_name")
                result["audio_stream"] = stream.get("index")
        fmt = data.get("format", {})
        result["duration"] = float(fmt.get("duration", 0))
    except Exception as e:
        logger.warning("ffprobe failed for %s: %s", path, e)
    return result


_FONT_CANDIDATES = [
    Path("C:/Windows/Fonts/arial.ttf"),
    Path("C:/Windows/Fonts/Arial.ttf"),
    Path("C:/Windows/Fonts/segoeui.ttf"),
    Path("C:/Windows/Fonts/calibri.ttf"),
]


def _get_font_path() -> Optional[Path]:
    for p in _FONT_CANDIDATES:
        if p.exists():
            logger.info("Using font: %s", p.resolve())
            return p.resolve()
    logger.warning("No font found, subtitles will be disabled")
    return None


class VideoAssembler:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.output_dir = self.settings.outputs_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def assemble(
        self,
        image_paths: list[Path],
        audio_path: Path,
        srt_path: Path,
        script: str,
        topic: str,
        output_path: Optional[Path] = None,
    ) -> Path:
        if output_path is None:
            output_path = self.output_dir / "final_video.mp4"

        if not image_paths:
            raise ValueError("No images provided")
        if not audio_path.exists():
            raise FileNotFoundError(f"Audio not found: {audio_path}")

        total_duration = self._get_audio_duration(audio_path)
        if total_duration <= 0:
            total_duration = self._estimate_duration(script)

        logger.info("START movie assembly — %d frames, %.1f seconds",
                     len(image_paths), total_duration)

        t0 = time.time()

        # ── Build video with MoviePy ───────────────────────────────
        from moviepy import (
            AudioFileClip,
            ImageClip,
            concatenate_videoclips,
            CompositeVideoClip,
            TextClip,
            vfx,
        )
        from moviepy.video.fx import Resize

        clips: list[ImageClip] = []
        display_time = total_duration / len(image_paths)
        fade = min(0.3, display_time * 0.15)

        for i, path in enumerate(image_paths):
            clip = (
                ImageClip(str(path))
                .resized((WIDTH, HEIGHT))
                .with_duration(display_time)
                .with_position("center")
            )
            if fade > 0 and i > 0:
                clip = clip.with_effects([vfx.FadeIn(fade)])
            if fade > 0 and i < len(image_paths) - 1:
                clip = clip.with_effects([vfx.FadeOut(fade)])
            clips.append(clip)

        video = concatenate_videoclips(clips, method="compose")

        # ── Burn subtitles ─────────────────────────────────────────
        sub_clips: list[TextClip] = []
        if srt_path.exists():
            try:
                font_path = _get_font_path()
                if font_path is None:
                    raise RuntimeError("no font available")

                subs: list[tuple[float, float, str]] = []
                with open(srt_path, encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines:
                    if "-->" in line:
                        parts = line.strip().split(" --> ")
                        start_parts = parts[0].replace(",", ".").split(":")
                        end_parts = parts[1].replace(",", ".").split(":")
                        start = (
                            int(start_parts[0]) * 3600
                            + int(start_parts[1]) * 60
                            + float(start_parts[2])
                        )
                        end = (
                            int(end_parts[0]) * 3600
                            + int(end_parts[1]) * 60
                            + float(end_parts[2])
                        )
                        subs.append((start, end, ""))
                    elif line.strip() and subs:
                        subs[-1] = (subs[-1][0], subs[-1][1], line.strip())

                for start, end, text in subs:
                    if text:
                        tc = (
                            TextClip(
                                text=text,
                                font=str(font_path),
                                font_size=28,
                                color="white",
                                stroke_color="black",
                                stroke_width=2,
                            )
                            .with_start(start)
                            .with_duration(end - start)
                            .with_position(("center", "bottom"))
                        )
                        sub_clips.append(tc)

                if sub_clips:
                    video = CompositeVideoClip([video, *sub_clips])
            except Exception:
                logger.warning(
                    "Subtitle rendering failed, continuing without subtitles",
                    exc_info=True,
                )

        # ── Add narration audio ────────────────────────────────────
        audio_clip = None
        has_audio = False
        if not audio_path.exists():
            logger.warning("Audio file not found, creating video without audio")
        else:
            try:
                audio_info = _probe(audio_path)
                logger.info(
                    "Audio file: %s  Duration: %.1fs  Sample rate: %s",
                    audio_path, audio_info.get("duration", 0),
                    audio_info.get("audio_codec", "unknown"),
                )
                audio_clip = AudioFileClip(str(audio_path))
                has_audio = True
            except Exception:
                logger.error("Failed to load audio, creating video without audio",
                             exc_info=True)

        if has_audio and audio_clip is not None:
            final = video.with_audio(audio_clip)
            logger.info("Audio attached successfully")
        else:
            final = video

        # ── Export ─────────────────────────────────────────────────
        final.write_videofile(
            str(output_path),
            codec="libx264",
            audio_codec="aac",
            fps=24,
            preset="ultrafast",
        )

        final.close()
        video.close()
        if audio_clip is not None:
            audio_clip.close()
        for c in clips:
            c.close()
        for c in sub_clips:
            c.close()

        elapsed = time.time() - t0
        logger.info("END movie assembly — export took %.1fs", elapsed)

        # ── ffprobe verification ───────────────────────────────────
        info = _probe(output_path)
        logger.info("export duration=%.1fs", elapsed)
        logger.info(
            "PROBE width=%s height=%s video_codec=%s audio_codec=%s duration=%.1fs",
            info.get("width"), info.get("height"),
            info.get("video_codec"), info.get("audio_codec"),
            info.get("duration", 0),
        )

        has_video = info.get("width") is not None and info.get("height") is not None
        has_audio = info.get("audio_codec") is not None
        has_duration = info.get("duration", 0) > 0

        errors: list[str] = []
        if not has_video:
            errors.append(f"no video stream (expected {WIDTH}x{HEIGHT})")
        if not has_audio:
            errors.append("no audio stream")
        if not has_duration:
            errors.append("duration is zero")
        if info.get("width") != WIDTH or info.get("height") != HEIGHT:
            errors.append(
                f"resolution {info.get('width')}x{info.get('height')} "
                f"!= expected {WIDTH}x{HEIGHT}"
            )
        if errors:
            raise RuntimeError(
                f"Post-export verification failed: {'; '.join(errors)}"
            )

        # ── Debug report ───────────────────────────────────────────
        real_count = sum(1 for p in image_paths if "_placeholder" not in p.name)
        placeholder_count = len(image_paths) - real_count
        print()
        print("=" * 60)
        print("  VIDEO ASSEMBLY REPORT")
        print("=" * 60)
        print(f"  Images used:         {len(image_paths)}")
        print(f"  Placeholders used:   {placeholder_count}")
        print(f"  Final resolution:    {info['width']}x{info['height']}")
        print(f"  Final duration:      {info.get('duration', 0):.1f}s")
        print(f"  Video codec:         {info.get('video_codec')}")
        print(f"  Audio codec:         {info.get('audio_codec')}")
        print(f"  Audio present:       {has_audio}")
        print(f"  Video path:          {output_path}")
        print(f"  Export time:         {elapsed:.1f}s")
        print("=" * 60)
        print()

        logger.info("Final video assembled: %s", output_path)
        return output_path

    def _get_audio_duration(self, audio_path: Path) -> float:
        info = _probe(audio_path)
        return info.get("duration", 0.0)

    def _estimate_duration(self, script: str) -> float:
        return max(15.0, len(script.split()) / 2.5)
