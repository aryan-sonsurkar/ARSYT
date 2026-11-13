from __future__ import annotations

import logging
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Optional

from config.settings import get_settings

logger = logging.getLogger(__name__)


def _find_py311() -> Optional[str]:
    candidates = [
        ["py", "-3.11"],
        [
            "C:\\Users\\Aryan Sonsurkar\\AppData\\Local\\Programs\\Python\\Python311\\python.exe"
        ],
    ]
    for cmd in candidates:
        try:
            r = subprocess.run(
                cmd + ["-c", "print('ok')"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if r.returncode == 0 and r.stdout.strip() == "ok":
                logger.debug("Found Python 3.11: %s", " ".join(cmd))
                return cmd[0]
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return None


class VoiceGenerator:
    KOKORO_RUNNER = (
        get_settings().project_root / "tools" / "kokoro_runner.py"
    )

    def __init__(self) -> None:
        self.settings = get_settings()
        self._py311 = _find_py311()
        self._kokoro_available = self._check_kokoro()

        if self._kokoro_available:
            logger.info("Kokoro TTS available (Python 3.11)")
        else:
            logger.warning("Kokoro TTS not available, using gTTS fallback")

    def _check_kokoro(self) -> bool:
        if self._py311 is None or not self.KOKORO_RUNNER.exists():
            logger.debug(
                "Kokoro unavailable: py311=%s runner=%s",
                self._py311,
                self.KOKORO_RUNNER.exists(),
            )
            return False
        try:
            r = subprocess.run(
                [self._py311, "-3.11" if self._py311 == "py" else "", "-c",
                 "from kokoro import KPipeline; print('ok')"]
                if self._py311 == "py"
                else [self._py311, "-c",
                      "from kokoro import KPipeline; print('ok')"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            result = r.returncode == 0 and r.stdout.strip() == "ok"
            if not result:
                logger.debug(
                    "Kokoro import check failed: rc=%d stderr=%s",
                    r.returncode,
                    r.stderr[:200],
                )
            return result
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.debug("Kokoro import check exception: %s", e)
            return False

    def _generate_kokoro(self, text: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if self._py311 == "py":
            cmd = [
                "py",
                "-3.11",
                str(self.KOKORO_RUNNER),
                "--text",
                text,
                "--output",
                str(output_path),
                "--voice",
                "am_michael",
            ]
        else:
            cmd = [
                self._py311,
                str(self.KOKORO_RUNNER),
                "--text",
                text,
                "--output",
                str(output_path),
                "--voice",
                "am_michael",
            ]

        logger.info("Kokoro generation start")
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Kokoro runner failed (rc={result.returncode}): "
                f"{result.stderr[:500]}"
            )

        if not output_path.exists():
            raise RuntimeError(
                f"Kokoro runner reported success but no output at "
                f"{output_path}"
            )

        logger.info("Kokoro generation end: %s", output_path)
        return output_path

    def _generate_gtts(self, text: str, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            from gtts import gTTS
        except ImportError:
            raise ImportError(
                "gTTS not installed. Run: pip install gtts"
            )
        tts = gTTS(text=text, lang="en", slow=False)
        tts.save(str(output_path))
        logger.info("Voice generated via gTTS: %s", output_path)
        return output_path

    def generate(
        self, script: str, output_path: Optional[Path] = None
    ) -> Path:
        if output_path is None:
            output_path = self.settings.voices_dir / "narration.wav"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        if self._kokoro_available:
            try:
                return self._generate_kokoro(script, output_path)
            except Exception:
                logger.error(
                    "Kokoro generation failed, falling back to gTTS"
                )
                traceback.print_exc()
                return self._generate_gtts(script, output_path)

        return self._generate_gtts(script, output_path)
