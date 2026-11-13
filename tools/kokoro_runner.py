"""Generate Kokoro TTS audio via Python 3.11 subprocess.

Usage:
    py -3.11 tools/kokoro_runner.py --text "Hello world" --output out.wav [--voice am_michael]
"""

import argparse
import logging
import sys
import time

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("kokoro_runner")

_kokoro_pipeline = None


def get_pipeline():
    global _kokoro_pipeline
    if _kokoro_pipeline is None:
        logger.info("Kokoro model load start")
        from kokoro import KPipeline
        _kokoro_pipeline = KPipeline(lang_code="a")
        logger.info("Kokoro model load success")
    return _kokoro_pipeline


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--voice", default="am_michael")
    args = parser.parse_args()

    pipeline = get_pipeline()

    logger.info("Generation start")
    gen_start = time.monotonic()
    import soundfile as sf

    generator = pipeline(args.text, voice=args.voice)
    audio = None
    for gs, ps, chunk in generator:
        sf.write(args.output, chunk, 24000)
        audio = chunk
        break

    gen_dur = time.monotonic() - gen_start
    audio_dur = len(audio) / 24000 if audio is not None else 0
    logger.info("Generation end: %.2fs", gen_dur)
    logger.info("Audio duration: %.2fs", audio_dur)


if __name__ == "__main__":
    main()
