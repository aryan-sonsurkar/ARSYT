from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Optional

from config.settings import get_settings
from database.repository import Repository

logger = logging.getLogger(__name__)


class QualityAgent:
    """Reviews generated content and scores it before production use."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()

    def score_script(self, title: str, script: str, topic: str, category: str) -> dict:
        scores = {
            "hook": self._score_hook(script),
            "educational_value": self._score_educational(script),
            "originality": self._score_originality(script, topic),
            "engagement_potential": self._score_engagement(script),
            "clarity": self._score_clarity(script),
        }
        overall = sum(scores.values()) / len(scores)
        result = {
            "scores": scores,
            "overall": round(overall, 2),
            "passed": overall >= self.settings.quality_threshold,
            "title": title,
            "topic": topic,
            "category": category,
            "script_length": len(script.split()),
        }
        self.db.save_quality_score(topic, category, result)
        logger.info(
            "Quality score for '%s': %.1f/10 (threshold: %.1f) %s",
            title, overall, self.settings.quality_threshold,
            "PASSED" if result["passed"] else "REJECTED",
        )
        return result

    def _score_hook(self, script: str) -> float:
        first_120 = script[:120].strip()
        score = 5.0
        hook_patterns = [
            r"\?$", r"(!|\.\.\.)$", r"^(Did you know|Ever wondered|What if|Here's why|The truth about|Why|How|The real reason)",
            r"\b(surprising|shocking|unexpected|crazy|genius|secret|hidden|deadly|brilliant)\b",
            r"\b(say goodbye|stop doing|start doing|you need to know|nobody tells you)\b",
        ]
        for pat in hook_patterns:
            if re.search(pat, first_120, re.IGNORECASE):
                score += 1.0
        return min(10.0, score)

    def _score_educational(self, script: str) -> float:
        score = 5.0
        edu_terms = [
            r"\b(because|therefore|however|explain|meaning|definition|example|specifically)\b",
            r"\b(algorithm|function|method|protocol|framework|architecture|pattern|principle)\b",
            r"\b(how|what|why|when)\b",
            r"\d+%", r"\d+x",
        ]
        for pat in edu_terms:
            if re.search(pat, script, re.IGNORECASE):
                score += 0.7
        word_count = len(script.split())
        if 60 <= word_count <= 160:
            score += 1.0
        return min(10.0, score)

    def _score_originality(self, script: str, topic: str) -> float:
        score = 6.0
        generic = [
            r"\b(important|essential|crucial|very|really|basically|actually|just|simply)\b",
        ]
        for pat in generic:
            matches = re.findall(pat, script, re.IGNORECASE)
            score -= len(matches) * 0.3
        if len(set(script.lower().split())) / max(len(script.split()), 1) > 0.6:
            score += 1.0
        return max(1.0, min(10.0, score))

    def _score_engagement(self, script: str) -> float:
        score = 5.0
        engage_pats = [
            r"\?$", r"!", r"\b(imagine|picture this|think about|what would you|have you ever)\b",
            r"\b(comment|subscribe|follow|share|like|watch|check out)\b",
            r"\b(fascinating|mind-blowing|game-changing|revolutionary|incredible)\b",
        ]
        for pat in engage_pats:
            matches = re.findall(pat, script, re.IGNORECASE)
            score += min(len(matches) * 0.5, 2.0)
        if script.strip().endswith(("?", "!", "...")):
            score += 1.0
        return min(10.0, score)

    def _score_clarity(self, script: str) -> float:
        score = 7.0
        words = script.split()
        avg_word_len = sum(len(w) for w in words) / max(len(words), 1)
        if avg_word_len > 7:
            score -= 2.0
        elif avg_word_len > 5:
            score -= 1.0
        long_sentences = len([s for s in script.split(".") if len(s.split()) > 30])
        score -= long_sentences * 0.5
        return max(1.0, min(10.0, score))

    def review_and_regenerate(
        self, topic_data: dict, script_data: dict, max_retries: int = 3
    ) -> tuple[dict, dict, dict]:
        from src.content_generator import ContentGenerator
        from src.script_generator import ScriptGenerator
        content_gen = ContentGenerator()
        script_gen = ScriptGenerator()

        current_topic = topic_data
        current_script = script_data
        last_score = None

        for attempt in range(max_retries):
            result = self.score_script(
                current_script["title"],
                current_script["script"],
                current_topic["topic"],
                current_topic["category"],
            )
            last_score = result
            if result["passed"]:
                logger.info("Content passed quality check on attempt %d/%d", attempt + 1, max_retries)
                return current_topic, current_script, result

            logger.warning(
                "Content failed quality check (%.1f). Regenerating... (attempt %d/%d)",
                result["overall"], attempt + 1, max_retries,
            )
            from src.content_generator import CATEGORIES
            used_topics = {t.topic for t in self.db.get_unused_topics()}
            weights = None
            if self.settings.strategy_enabled:
                try:
                    from src.agents.strategy_agent import StrategyAgent
                    weights = StrategyAgent().get_weighted_categories()
                except Exception:
                    pass
            current_topic = content_gen.generate_topic_and_content(used_topics, weights)
            current_script = script_gen.generate_script(
                current_topic["topic"], current_topic["category"], current_topic["content"],
            )

        logger.error(
            "Content failed quality check after %d attempts. Using last generation.",
            max_retries,
        )
        return current_topic, current_script, last_score
