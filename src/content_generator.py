from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urljoin

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from config.settings import get_settings
from src.topic_intelligence import TopicIntelligenceEngine

logger = logging.getLogger(__name__)

CATEGORIES = [
    "mindblowing_concepts",
    "career_secrets",
    "hidden_truths",
    "beginner_mistakes",
    "engineering_marvels",
    "productivity_hacks",
]


class ContentGenerator:
    """Generates topic and script content using the TopicIntelligenceEngine
    for topic selection and Ollama for content expansion."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.api_url = urljoin(
            self.settings.ollama_host.rstrip("/") + "/", "api/generate"
        )
        self.model = self.settings.content_model or self.settings.ollama_model
        self._used_topics: set[str] = set()
        self.engine = TopicIntelligenceEngine()

    def set_used_topics(self, topics: set[str]) -> None:
        self._used_topics = topics

    def _is_retryable(exc: BaseException) -> bool:
        return isinstance(exc, (requests.ConnectionError, requests.Timeout))

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_retryable),
    )
    def _ollama_generate(self, prompt: str, temp: float = 0.8, max_tokens: int = 1024) -> str:
        import time
        start = time.monotonic()
        logger.info(
            "Ollama request: model=%s prompt_size=%d max_tokens=%d",
            self.model, len(prompt), max_tokens,
        )
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temp, "num_predict": max_tokens},
        }
        resp = requests.post(
            self.api_url,
            json=payload,
            timeout=self.settings.ollama_timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        duration = time.monotonic() - start
        logger.info(
            "Ollama response: model=%s duration=%.2fs response_length=%d",
            self.model, duration, len(text),
        )
        return text

    def generate_content(self, topic: str, category: str, hook: str = "", angle: str = "") -> str:
        import time
        stage_start = time.monotonic()
        hook_hint = f"\nHook to use: {hook}" if hook else ""
        angle_hint = f"\nVideo angle: {angle}" if angle else ""

        prompt = f"""You are writing a YouTube Shorts script that a 16-year-old would NOT scroll past.

Topic: {topic}
Category: {category}{hook_hint}{angle_hint}

STRUCTURE:
1. First sentence creates tension (hook). Never start with definitions, history, or company descriptions.
2. Second sentence increases curiosity.
3. Third sentence starts the explanation.
4. End with a call to action.

REQUIREMENTS:
- 150-250 words
- Conversational, fast-paced tone
- Easy to visualize
- Include a surprising or counter-intuitive point

Write the content only."""
        result = self._ollama_generate(prompt)
        stage_dur = time.monotonic() - stage_start
        logger.info(
            "STAGE content generation: model=%s duration=%.2fs response_length=%d",
            self.model, stage_dur, len(result),
        )
        return result

    def generate_topic_and_content(
        self,
        used_topics: Optional[set[str]] = None,
        weights: Optional[dict[str, float]] = None,
    ) -> dict:
        if used_topics:
            self.set_used_topics(used_topics)

        # Use the TopicIntelligenceEngine to find the best topic
        topic_data = self.engine.generate_topic(
            used_topics=self._used_topics,
            weights=weights,
        )

        content = self.generate_content(
            topic=topic_data["topic"],
            category=topic_data.get("category", "programming_concepts"),
            hook=topic_data.get("hook", ""),
            angle=topic_data.get("angle", ""),
        )

        topic_data["content"] = content
        logger.info(
            "Generated content for topic: %s [%s] (score: %.2f)",
            topic_data["topic"],
            topic_data.get("category", ""),
            topic_data.get("overall_score", 0),
        )
        return topic_data
