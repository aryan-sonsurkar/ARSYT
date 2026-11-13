from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


class CaptionGenerator:
    def __init__(self) -> None:
        self.settings = get_settings()

    def generate_srt(
        self, script: str, total_duration: float, output_path: Optional[Path] = None
    ) -> Path:
        if output_path is None:
            output_path = self.settings.subtitles_dir / "captions.srt"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        words = script.split()
        if not words:
            self._write_empty_srt(output_path)
            return output_path

        num_words = len(words)
        words_per_segment = max(3, min(7, math.ceil(num_words / 20)))
        segments = [
            words[i : i + words_per_segment]
            for i in range(0, num_words, words_per_segment)
        ]
        num_segments = len(segments)
        segment_duration = total_duration / num_segments if num_segments > 0 else total_duration

        srt_lines: list[str] = []
        current_time = 0.0

        for idx, segment in enumerate(segments):
            seg_text = " ".join(segment)
            start_time = current_time
            duration = segment_duration * (len(segment) / words_per_segment)
            end_time = min(start_time + duration, total_duration)
            if end_time - start_time < 0.5:
                end_time = start_time + 0.5
            if end_time > total_duration:
                end_time = total_duration

            srt_lines.append(str(idx + 1))
            srt_lines.append(self._format_time(start_time) + " --> " + self._format_time(end_time))
            srt_lines.append(seg_text)
            srt_lines.append("")

            current_time = end_time

        srt_content = "\n".join(srt_lines)
        output_path.write_text(srt_content, encoding="utf-8")
        logger.info(
            "Generated SRT with %d segments, duration %.1fs: %s",
            num_segments,
            total_duration,
            output_path,
        )
        return output_path

    def _format_time(self, seconds: float) -> str:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace(".", ",")

    def _write_empty_srt(self, path: Path) -> None:
        path.write_text(
            "1\n00:00:00,000 --> 00:00:05,000\nNo caption available\n",
            encoding="utf-8",
        )
        logger.warning("Empty script, wrote placeholder SRT")
