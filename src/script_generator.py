from __future__ import annotations

import json
import logging
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from config.settings import get_settings
from database.repository import Repository

logger = logging.getLogger(__name__)


class ScriptGenerator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self.api_url = (
            self.settings.ollama_host.rstrip("/") + "/api/generate"
        )
        self.model = self.settings.script_model or self.settings.ollama_model
        self.db = Repository()

    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, (requests.ConnectionError, requests.Timeout))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
    )
    def _ollama_generate(self, prompt: str, temperature: float = 0.7) -> str:
        import time
        start = time.monotonic()
        logger.info(
            "Ollama request: model=%s prompt_size=%d",
            self.model, len(prompt),
        )
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": 2048,
            },
        }
        resp = requests.post(
            self.api_url,
            json=payload,
            timeout=self.settings.ollama_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data.get("response", "").strip()
        duration = time.monotonic() - start
        logger.info(
            "Ollama response: model=%s duration=%.2fs response_length=%d",
            self.model, duration, len(text),
        )
        return text

    def generate_script(
        self, topic: str, category: str, content: str
    ) -> dict:
        import time
        stage_start = time.monotonic()
        prompt = f"""You are a professional script writer for viral coding YouTube Shorts (30-60 seconds).

Convert this content into a script that makes a 16-year-old interested in coding stop scrolling.

TOPIC: {topic}
CATEGORY: {category}
CONTENT: {content}

HOOK RULES:
1. First sentence creates tension — never start with definitions, history, or company descriptions
2. Second sentence increases curiosity
3. Third sentence starts the explanation

STRICT REQUIREMENTS:
1. Script must be 30-60 seconds when spoken (~75-150 words)
2. Educational and engaging tone, conversational
3. Easy to visualize
4. End with a clear call to action
5. NO markdown, NO stage directions, NO parentheticals

Output ONLY valid JSON with this exact structure:
{{
  "title": "Catchy short title (max 60 chars)",
  "script": "The full narration script, one paragraph",
  "description": "2-3 sentence video description for upload",
  "hashtags": "#Hashtag1 #Hashtag2 #Hashtag3 #Hashtag4 #Hashtag5"
}}

No explanations, no backticks, just the JSON."""
        response = self._ollama_generate(prompt, temperature=0.7)
        response = (
            response.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )

        try:
            data = json.loads(response)
            required = {"title", "script", "description", "hashtags"}
            if not required.issubset(data.keys()):
                raise ValueError(f"Missing keys: {required - data.keys()}")
            if not data["title"] or not data["script"]:
                raise ValueError("Empty title or script")
            stage_dur = time.monotonic() - stage_start
            logger.info(
                "STAGE script generation: model=%s duration=%.2fs response_length=%d",
                self.model, stage_dur, len(response),
            )
            logger.info(
                "Generated script: %s (%d words)",
                data["title"],
                len(data["script"].split()),
            )
            return data
        except (json.JSONDecodeError, ValueError) as e:
            stage_dur = time.monotonic() - stage_start
            logger.warning(
                "STAGE script generation (fallback): model=%s duration=%.2fs",
                self.model, stage_dur,
            )
            logger.warning(
                "Script JSON parse failed, attempting raw: %s", e
            )
            return {
                "title": topic,
                "script": response[:500] if response else content[:500],
                "description": f"Learn about {topic} in this short video!",
                "hashtags": "#coding #programming #tech",
            }
