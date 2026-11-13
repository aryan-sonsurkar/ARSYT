from __future__ import annotations

from pathlib import Path

from src.caption_generator import CaptionGenerator


class TestCaptionGenerator:
    def test_generate_srt_creates_file(self, tmp_path):
        gen = CaptionGenerator()
        output = tmp_path / "test.srt"
        gen.generate_srt(
            "This is a test script for caption generation.",
            total_duration=10.0,
            output_path=output,
        )
        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert "1" in content
        assert "-->" in content
        assert "caption" in content.lower() or "test" in content.lower()

    def test_format_time(self):
        gen = CaptionGenerator()
        result = gen._format_time(65.5)
        assert "00:01:05" in result
        assert "," in result

    def test_empty_script(self, tmp_path):
        gen = CaptionGenerator()
        output = tmp_path / "empty.srt"
        gen.generate_srt("", total_duration=5.0, output_path=output)
        assert output.exists()
        content = output.read_text(encoding="utf-8")
        assert "No caption" in content
