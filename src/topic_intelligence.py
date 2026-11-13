from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from config.settings import get_settings
from database.repository import Repository
from src.topic_sources import GitHubTrending, HackerNews, DevTo, RedditProgramming

logger = logging.getLogger(__name__)

CATEGORIES = [
    "mindblowing_concepts",
    "career_secrets",
    "hidden_truths",
    "beginner_mistakes",
    "engineering_marvels",
    "productivity_hacks",
]

AVOID_KEYWORDS = [
    "repository", "repo", "open source", "maintenance",
    "release notes", "changelog", "update", "patch",
    "framework", "library", "showcase", "demo project",
    "company", "enterprise", "corporate", "codebase",
]

SCORE_DIMENSIONS = [
    "curiosity_score",
    "surprise_score",
    "career_value_score",
    "beginner_relevance_score",
    "shareability_score",
]

TENSION_HOOK_PATTERNS = [
    "Most developers don't realize...",
    "The mistake that...",
    "Here's why...",
    "You've probably been told...",
    "This simple trick...",
]


class TopicIntelligenceEngine:
    """Generates YouTube Shorts topics optimized for retention and shareability."""

    def __init__(self) -> None:
        self.settings = get_settings()
        self.db = Repository()
        self._ollama_url = (
            self.settings.ollama_host.rstrip("/") + "/api/generate"
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: isinstance(e, (requests.ConnectionError, requests.Timeout))),
    )
    def _ollama(self, prompt: str, temp: float = 0.8, max_tokens: int = 2048) -> str:
        model = self.settings.topic_model
        prompt_size = len(prompt)
        start = time.monotonic()
        logger.info(
            "Ollama request: model=%s prompt_size=%d max_tokens=%d",
            model, prompt_size, max_tokens,
        )
        resp = requests.post(
            self._ollama_url,
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": temp, "num_predict": max_tokens},
            },
            timeout=self.settings.ollama_timeout,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        duration = time.monotonic() - start
        logger.info(
            "Ollama response: model=%s duration=%.2fs response_length=%d",
            model, duration, len(text),
        )
        if duration > 60:
            logger.warning(
                "Ollama request exceeded 60s: model=%s duration=%.2fs prompt_size=%d max_tokens=%d",
                model, duration, prompt_size, max_tokens,
            )
        return text

    def _clean_json(self, raw: str) -> str:
        return (
            raw.strip()
            .removeprefix("```json").removeprefix("```")
            .removesuffix("```").strip()
        )

    def _extract_json_array(self, text: str) -> list:
        """Parse JSON array from LLM response, with error recovery."""
        cleaned = self._clean_json(text)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for key in ("candidates", "topics", "hooks", "items"):
                    if key in parsed and isinstance(parsed[key], list):
                        return parsed[key]
        except (json.JSONDecodeError, ValueError):
            pass
        # Try to find array with regex
        match = re.search(r"\[.*?\]", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                pass
        return []

    def fetch_source_topics(self) -> list[dict]:
        """Pull fresh topics from all configured external sources."""
        if not self.settings.topic_sources_enabled:
            logger.info("Topic sources disabled, skipping fetch")
            return []

        sources = [
            ("GitHub", GitHubTrending()),
            ("HN", HackerNews()),
            ("Dev.to", DevTo()),
            ("Reddit", RedditProgramming()),
        ]

        all_topics: list[dict] = []
        for name, fetcher in sources:
            try:
                items = fetcher.fetch()
                for item in items:
                    item["source_name"] = name
                all_topics.extend(items)
                logger.info("%s returned %d items", name, len(items))
            except Exception as e:
                logger.warning("%s fetch failed: %s", name, e)

        for item in all_topics:
            self.db.insert_source_topic(item)

        logger.info("Fetched %d source topics total", len(all_topics))
        return all_topics

    def deduplicate_and_rank(self, sources: list[dict], used: set[str]) -> list[dict]:
        """Remove duplicates and used topics, rank by source score."""
        seen_titles: set[str] = set()
        ranked: list[dict] = []

        for item in sources:
            title = (item.get("title") or item.get("topic", "")).strip().lower()
            if not title or len(title) < 10:
                continue
            if title in seen_titles:
                continue
            seen_titles.add(title)
            if any(u.lower() in title or title in u.lower() for u in used):
                continue

            score = item.get("score", 0) or 0
            if isinstance(score, (int, float)):
                item["rank_score"] = float(score)
            else:
                item["rank_score"] = 0.0

            ranked.append(item)

        ranked.sort(key=lambda x: x["rank_score"], reverse=True)
        logger.info("Ranked %d unique source topics", len(ranked))
        return ranked[:30]

    def generate_candidates(
        self, ranked_sources: list[dict], weights: Optional[dict[str, float]] = None
    ) -> list[dict]:
        """Generate candidate topics optimized for Shorts retention."""
        if not ranked_sources:
            logger.warning("No source topics available, falling back to LLM-only generation")
            return self._fallback_candidates(weights)

        logger.info("START candidate generation (source-based)")

        source_block = "\n\n".join(
            f"[{s.get('source_name', 'Source')}] {s.get('title', '')} "
            f"(score: {s.get('rank_score', 0):.0f})"
            for i, s in enumerate(ranked_sources[:10])
        )

        weight_hint = ""
        if weights:
            best_cat = max(weights, key=weights.get)
            weight_hint = f"\nPreferred category (weight higher): {best_cat}"

        prompt = f"""You are a topic strategist for a YouTube Shorts channel about coding. Your goal is to pick topics that make a 16-year-old interested in coding STOP SCROLLING.

Source material (real developer discussions):
{source_block}{weight_hint}

CATEGORIES (pick one per topic):
{chr(10).join(f'- {c}' for c in CATEGORIES)}

REQUIREMENTS for each topic:
- Must satisfy at least ONE: curiosity, surprise, career gain, productivity gain, hidden truth, beginner mistake, mental model, real-world engineering secret
- 6-10 words, punchy, specific
- Creates a curiosity gap (viewer NEEDS to know the answer)

ABSOLUTELY AVOID:
- Repository updates, company codebases, framework release notes
- Open-source maintenance stories, generic project showcases
- Anything that starts with "How [Company]" or "[Project] updates"
- Tutorials, walkthroughs, "what is" explanations
- Unless there is a surprising lesson underneath

Output a JSON array of EXACTLY 5 objects:
[{{"topic": "...", "category": "...", "hook": "one-sentence hook creating tension", "angle": "one-sentence explanation angle"}}]

No markdown. Valid JSON only."""

        response = self._ollama(prompt, temp=0.85, max_tokens=512)
        candidates = self._extract_json_array(response)

        valid = []
        for c in candidates[:5]:
            topic = (c.get("topic") or "").strip()
            cat = (c.get("category") or "").strip()
            if topic and cat in CATEGORIES:
                # Check avoid keywords
                topic_lower = topic.lower()
                if any(kw in topic_lower for kw in AVOID_KEYWORDS):
                    logger.debug("Skipping avoid-match topic: %s", topic)
                    continue
                valid.append(c)

        if not valid:
            logger.warning("No valid candidates from sources, falling back")
            return self._fallback_candidates(weights)

        logger.info("END candidate generation (source-based): %d candidates", len(valid))
        return valid[:5]

    def _fallback_candidates(self, weights: Optional[dict] = None) -> list[dict]:
        """Pure LLM fallback when no external sources available."""
        logger.info("START candidate generation (fallback/LLM-only)")

        weight_hint = ""
        if weights:
            best = max(weights, key=weights.get)
            weight_hint = f"\nPreferred category: {best}"

        prompt = f"""Generate 5 video topics for coding YouTube Shorts that a 16-year-old would NOT scroll past.

CATEGORIES (pick one per topic):
{chr(10).join(f'- {c}' for c in CATEGORIES)}{weight_hint}

Each topic must create a curiosity gap. Must satisfy at least ONE of: curiosity, surprise, career gain, productivity gain, hidden truth, beginner mistake, mental model, real-world engineering secret.

AVOID: repository updates, company codebases, framework releases, open-source maintenance, generic showcases, tutorials, "what is" explanations.

Output JSON array:
[{{"topic": "...", "category": "...", "hook": "tension-creating hook", "angle": "explanation angle"}}]

No markdown. Valid JSON only."""

        response = self._ollama(prompt, temp=0.9, max_tokens=512)
        candidates = self._extract_json_array(response)

        valid = []
        for c in candidates[:5]:
            topic = (c.get("topic") or "").strip()
            cat = (c.get("category") or "").strip()
            topic_lower = topic.lower()
            if topic and cat in CATEGORIES and not any(kw in topic_lower for kw in AVOID_KEYWORDS):
                valid.append(c)

        logger.info("END candidate generation (fallback): %d candidates", len(valid))
        return valid[:5]

    def _generate_hooks_for_topic(self, topic: str, category: str) -> list[dict]:
        """Generate 10 hooks for a topic, scored, best first."""
        prompt = f"""For this YouTube Shorts topic, generate 10 hooks that make people stop scrolling.

Topic: {topic}
Category: {category}

Use these patterns (vary them):
{chr(10).join(f'- {p}' for p in TENSION_HOOK_PATTERNS)}

RULES for each hook:
1. First sentence creates tension
2. Second sentence increases curiosity
3. Never start with definitions, history, or company descriptions

For each hook, score 0-10 on:
- curiosity: does it make you NEED to know?
- surprise: is there an unexpected element?
- career_value: does it help the viewer professionally?
- beginner_relevance: would a beginner care?
- shareability: would you send this to a friend?

Output JSON array of EXACTLY 10 objects sorted best first:
[{{"hook": "Full hook text (2-3 sentences)", "pattern": "A|B|C|D|E", "curiosity": 8, "surprise": 6, "career_value": 7, "beginner_relevance": 5, "shareability": 6}}]

No markdown. Valid JSON only."""

        response = self._ollama(prompt, temp=0.8, max_tokens=1024)
        hooks = self._extract_json_array(response)

        scored = []
        for h in hooks[:10]:
            hook_text = (h.get("hook") or "").strip()
            if not hook_text or len(hook_text) < 15:
                continue
            curiosity = min(10, max(0, int(h.get("curiosity", 5))))
            surprise = min(10, max(0, int(h.get("surprise", 5))))
            career_value = min(10, max(0, int(h.get("career_value", 5))))
            beginner = min(10, max(0, int(h.get("beginner_relevance", 5))))
            share = min(10, max(0, int(h.get("shareability", 5))))

            total = (
                curiosity * 3
                + surprise * 2
                + career_value * 2
                + beginner * 2
                + share
            )
            scored.append({
                "hook": hook_text,
                "pattern": h.get("pattern", ""),
                "scores": {
                    "curiosity_score": curiosity,
                    "surprise_score": surprise,
                    "career_value_score": career_value,
                    "beginner_relevance_score": beginner,
                    "shareability_score": share,
                },
                "total": total,
            })

        scored.sort(key=lambda x: x["total"], reverse=True)
        logger.debug(
            "Generated %d hooks for topic '%s', best=%.0f",
            len(scored), topic, scored[0]["total"] if scored else 0,
        )
        return scored

    def _score_topic(self, topic: str, hook: str) -> dict:
        """Score a topic+hook on the 5 retention dimensions."""
        prompt = f"""You evaluate coding YouTube Shorts topics for retention. Score this topic + hook.

Topic: {topic}
Hook: {hook}

Rate each dimension 0-10 (integer):

curiosity_score - does the topic create a gap between what the viewer knows and wants to know? (0=no curiosity, 10=must watch)
surprise_score - does the topic contain an unexpected or counter-intuitive element? (0=expected, 10=mind-blowing)
career_value_score - will this knowledge help the viewer in their job or career? (0=useless, 10=career-changing)
beginner_relevance_score - would a beginner (junior dev, student) find this valuable? (0=too advanced/narrow, 10=perfect for beginners)
shareability_score - would a developer share this with their team or friends? (0=forgettable, 10=must-share)

Output ONLY valid JSON: {{"curiosity_score": 0, "surprise_score": 0, "career_value_score": 0, "beginner_relevance_score": 0, "shareability_score": 0}}

No markdown. Just JSON."""

        response = self._ollama(prompt, temp=0.3, max_tokens=256)
        cleaned = self._clean_json(response)
        try:
            scores = json.loads(cleaned)
            for dim in SCORE_DIMENSIONS:
                if dim not in scores:
                    scores[dim] = 5
                scores[dim] = min(10, max(0, int(scores[dim])))
            return scores
        except (json.JSONDecodeError, ValueError, TypeError):
            logger.warning("Score parse failed for '%s', using defaults", topic)
            return {dim: 5 for dim in SCORE_DIMENSIONS}

    def _passes_16yo_test(self, topic: str, hook: str) -> bool:
        """Ask the LLM: would a 16-year-old interested in coding stop scrolling?"""
        prompt = f"""Question: Would a random 16-year-old interested in coding stop scrolling for this YouTube Shorts topic?

Topic: {topic}
Hook: {hook}

Answer ONLY "yes" or "no" then a one-sentence reason.

A "no" means: too boring, too niche, too corporate, no curiosity gap, no personal relevance.
A "yes" means: creates tension, curiosity, surprise, or clear benefit.

Answer:"""

        response = self._ollama(prompt, temp=0.2, max_tokens=100)
        answer = response.strip().lower()
        passes = answer.startswith("yes") or "would stop" in answer or "would watch" in answer
        reason = response.strip()[:200]
        logger.debug("16yo test: %s -> %s (reason: %s)", topic, "PASS" if passes else "FAIL", reason)
        return passes

    def score_candidates(self, candidates: list[dict]) -> list[dict]:
        """Score each candidate: generate hooks, score topic, apply 16yo test."""
        logger.info("START candidate scoring (%d candidates)", len(candidates))
        scored = []

        for c in candidates:
            topic = c.get("topic", "")
            category = c.get("category", "")

            if not topic:
                continue

            # Generate and score hooks
            hooks = self._generate_hooks_for_topic(topic, category)
            best_hook = hooks[0] if hooks else {"hook": c.get("hook", ""), "scores": {}}

            # Score the topic with best hook
            scores = self._score_topic(topic, best_hook["hook"])

            # Compute weighted total
            total = (
                scores["curiosity_score"] * 3
                + scores["surprise_score"] * 2
                + scores["career_value_score"] * 2
                + scores["beginner_relevance_score"] * 2
                + scores["shareability_score"]
            )

            passes_test = self._passes_16yo_test(topic, best_hook["hook"])
            if not passes_test:
                total *= 0.5  # Penalty for failing the test

            entry = {
                **c,
                "hook": best_hook["hook"],
                "hook_scores": best_hook["scores"],
                "all_hooks": hooks[:5],
                "scores": scores,
                "overall": round(total, 2),
                "passes_16yo_test": passes_test,
            }
            scored.append(entry)

        scored.sort(key=lambda x: x["overall"], reverse=True)

        for i, s in enumerate(scored):
            self.db.insert_topic_candidate(
                topic=s.get("topic", ""),
                category=s.get("category", ""),
                hook=s.get("hook", ""),
                angle=s.get("angle", ""),
                scores=s["scores"],
                overall=s["overall"],
                selected=(i == 0),
                source_inspiration=s.get("source_inspiration", ""),
            )

        if scored:
            logger.info(
                "END candidate scoring: winner='%s' score=%.2f passes_16yo=%s hooks=%d",
                scored[0]["topic"], scored[0]["overall"],
                scored[0]["passes_16yo_test"], len(scored[0].get("all_hooks", [])),
            )
        return scored

    def generate_topic(
        self,
        used_topics: Optional[set[str]] = None,
        weights: Optional[dict[str, float]] = None,
    ) -> dict:
        """Full pipeline: fetch -> rank -> generate candidates -> score -> pick winner."""
        pipeline_start = time.monotonic()
        used = used_topics or set()

        logger.info("=== TOPIC PIPELINE START ===")
        sources = self.fetch_source_topics()
        ranked = self.deduplicate_and_rank(sources, used)

        candidates = self.generate_candidates(ranked, weights)
        if not candidates:
            logger.error("No candidates generated")
            cat = "mindblowing_concepts"
            if weights:
                cat = max(weights, key=weights.get)
            return {"topic": "Why Most Coding Projects Fail After 1000 Lines", "category": cat, "content": ""}

        scored = self.score_candidates(candidates)
        if not scored:
            logger.error("All candidates failed scoring")
            cat = "mindblowing_concepts"
            if weights:
                cat = max(weights, key=weights.get)
            return {"topic": "The Database Mistake That Slows Everything Down", "category": cat, "content": ""}

        winner = scored[0]

        pipeline_duration = time.monotonic() - pipeline_start
        logger.info(
            "=== TOPIC PIPELINE END: winner='%s' score=%.2f passes_16yo=%s duration=%.2fs "
            "(from %d candidates) ===",
            winner["topic"], winner["overall"],
            winner.get("passes_16yo_test", False), pipeline_duration, len(scored),
        )

        return {
            "topic": winner["topic"],
            "category": winner.get("category", "mindblowing_concepts"),
            "hook": winner.get("hook", ""),
            "angle": winner.get("angle", ""),
            "overall_score": winner["overall"],
            "all_candidates": scored,
            "source_inspiration": winner.get("source_inspiration", ""),
            "passes_16yo_test": winner.get("passes_16yo_test", False),
        }
