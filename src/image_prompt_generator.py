from __future__ import annotations

import logging
import re
import time
from typing import Optional

import requests

from config.settings import get_settings

logger = logging.getLogger(__name__)

_LLM_SYSTEM = """You are a storyboard artist for a coding education YouTube channel.
Generate 8 visually distinct image prompts for a 9:16 vertical short.

Scenes:
1. Hook — eye-catching opener
2. Problem — the challenge this concept solves
3. Explanation — how it works visually
4. Visualization — data flow or process diagram
5. Example — concrete code or usage scene
6. Real-world use — practical application
7. Advanced concept — deeper technical insight
8. CTA — call to action / subscribe

Each prompt must include: scene description, lighting, composition, vertical 9:16 framing.
Use dark theme coding aesthetic with neon accent colors (teal, amber, cyan).
Return one prompt per line, numbered 1-8. No extra text."""

_DETERMINISTIC_TEMPLATES = [
    "A cinematic establishing shot of {topic} visualized as a glowing digital landscape, "
    "central subject emerging from streams of data and code, dramatic cinematic lighting with "
    "blue and amber highlights, highly detailed, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "A detailed infographic-style visualization explaining the core idea of {topic}, "
    "floating geometric shapes connected by luminous lines, dark theme with teal accent lighting, "
    "highly detailed technical illustration, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "A technical deep-dive visualization of {topic} showing internal mechanisms and components, "
    "layered translucent panels revealing how the system works, cinematic volumetric lighting, "
    "ultra detailed engineering artwork, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "A system architecture diagram for {topic} rendered as a 3D isometric visualization, "
    "connected services and data flows illuminated with neon blue light, depth of field, "
    "highly detailed infrastructure artwork, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "A close-up view of code implementing {topic}, syntax-highlighted lines floating in a dark "
    "terminal environment, green and cyan glow on a black background, shallow depth of field, "
    "highly detailed programming aesthetic, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "A real-world application scene showing {topic} in action, metaphor represented as a "
    "futuristic cityscape or machine with glowing operational parts, cinematic golden hour lighting, "
    "highly detailed concept art, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "An advanced visualization exploring deeper aspects of {topic}, layered holographic displays "
    "showing complex data relationships, dramatic rim lighting with purple and teal tones, "
    "highly detailed sci-fi technical artwork, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
    "A triumphant concluding visualization for {topic}, glowing achievement symbol at center, "
    "golden ambient lighting radiating outward, clean minimalist composition, "
    "highly detailed, professional tech aesthetic, vertical 9:16 composition, "
    "suitable for YouTube Shorts.",
]


def _clean_topic(raw: str) -> str:
    t = raw.strip().strip('"').strip("'")
    if len(t) > 80:
        t = t[:80].rsplit(" ", 1)[0]
    return t


def _fallback_prompts(topic: str, num: int = 8) -> list[str]:
    clean = _clean_topic(topic)
    return [t.format(topic=clean) for t in _DETERMINISTIC_TEMPLATES[:num]]


class ImagePromptGenerator:
    """LLM-powered storyboard prompt generator with deterministic fallback.

    Uses the lightweight ``topic_model`` (default ``llama3.2:3b``) with
    ``ollama_timeout`` and 512 max tokens.  Falls back to Python templates
    if the LLM call fails or times out.
    """

    def __init__(self) -> None:
        self.settings = get_settings()
        self.api_url = self.settings.ollama_host.rstrip("/") + "/api/generate"
        self.model = self.settings.topic_model or self.settings.ollama_model

    def generate(
        self,
        topic: str,
        content: str,
        script: str,
        num_prompts: int = 8,
    ) -> list[str]:
        start = time.monotonic()

        # Try LLM first with full script for rich scene context.
        llm_prompts = self._try_llm(topic, content, script, num_prompts)
        if llm_prompts is not None:
            duration = time.monotonic() - start
            logger.info(
                "LLM prompts: generated=%d duration=%.2fs",
                len(llm_prompts), duration,
            )
            return llm_prompts

        # Fallback to deterministic templates.
        logger.info("Using deterministic fallback templates")
        return _fallback_prompts(topic, num_prompts)

    def _try_llm(
        self,
        topic: str,
        content: str,
        script: str,
        num_prompts: int,
    ) -> Optional[list[str]]:
        user_prompt = (
            f"Topic: {topic}\n\n"
            f"Content (background):\n{content}\n\n"
            f"Script (narration):\n{script}\n\n"
            f"Generate {num_prompts} visual prompts for a vertical coding short:"
        )

        timeout = self.settings.ollama_timeout
        logger.info(
            "LLM request: model=%s prompt_size=%d timeout=%ds",
            self.model, len(user_prompt), timeout,
        )

        payload = {
            "model": self.model,
            "prompt": f"{_LLM_SYSTEM}\n\n{user_prompt}",
            "stream": False,
            "options": {
                "temperature": 0.8,
                "num_predict": 512,
            },
        }

        try:
            resp = requests.post(
                self.api_url,
                json=payload,
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            text = data.get("response", "").strip()
        except requests.Timeout:
            logger.warning("LLM timed out after %ds, using fallback", timeout)
            return None
        except Exception as e:
            logger.warning("LLM request failed: %s, using fallback", e)
            return None

        parsed = self._parse_llm_output(text, num_prompts)
        if len(parsed) < 4:
            logger.warning(
                "LLM returned only %d/%d prompts, using fallback",
                len(parsed), num_prompts,
            )
            return None

        return parsed[:num_prompts]

    @staticmethod
    def _parse_llm_output(text: str, expected: int) -> list[str]:
        lines = text.split("\n")
        prompts: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # Accept "1. text" or "1) text" or "1:text" or plain "text"
            match = re.match(r"^\d+[\.\):]\s*(.+)", stripped)
            if match:
                body = match.group(1).strip()
            else:
                body = stripped
            if len(body) > 20:
                prompts.append(body)
        return prompts
