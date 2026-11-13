from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception

from config.settings import get_settings
from database.models import Video

logger = logging.getLogger(__name__)

TITLE_PATTERNS = [
    "The {adjective} {noun} That {verb} Everything",
    "Why {common_belief} Is Actually {opposite}",
    "The Mistake Most Developers Make With {topic}",
    "Here's Why {common_practice} Doesn't Work",
    "You've Been Told {misconception}. Here's The Truth",
    "This Simple {technique} Will Change How You {action}",
    "The Hidden {concept} Behind {popular_thing}",
    "What Nobody Tells You About {topic}",
    "The {number} Lines Of Code That Broke {famous_system}",
    "Why {beginner_advice} Is Bad Advice",
]

CAPTION_TEMPLATES = [
    "What's the biggest {topic_related} you've ever built?",
    "Most developers never learn {concept} until it's too late.",
    "I spent {time_period} learning {topic}. Here's what I wish I knew.",
    "Drop a {emoji} if you've made this mistake before.",
    "How many of these did you know? {numbered_list}",
]


class PublishPackGenerator:
    def __init__(self) -> None:
        self.settings = get_settings()
        self._ollama_url = (
            self.settings.ollama_host.rstrip("/") + "/api/generate"
        )
        # Use topic_model (llama3.2:3b) — fast, good enough for creative generation
        self.model = self.settings.topic_model

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(lambda e: isinstance(e, (requests.ConnectionError, requests.Timeout))),
    )
    def _ollama(self, prompt: str, temp: float = 0.8, max_tokens: int = 2048) -> str:
        model = self.model
        start = time.monotonic()
        logger.info(
            "Ollama request: model=%s prompt_size=%d max_tokens=%d",
            model, len(prompt), max_tokens,
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
        logger.info(
            "Ollama response: model=%s duration=%.2fs response_length=%d",
            model, time.monotonic() - start, len(text),
        )
        return text

    def _clean_json(self, raw: str) -> str:
        return (
            raw.strip()
            .removeprefix("```json").removeprefix("```")
            .removesuffix("```").strip()
        )

    def _extract_json_array(self, text: str) -> list:
        cleaned = self._clean_json(text)
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict):
                for key in ("titles", "captions", "hashtags", "items", "sets"):
                    if key in parsed and isinstance(parsed[key], list):
                        return parsed[key]
        except (json.JSONDecodeError, ValueError):
            pass
        match = re.search(r"\[.*?\]", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except (json.JSONDecodeError, ValueError):
                pass
        return []

    def _generate_titles(self, video: Video) -> list[dict]:
        """Generate 10 viral title candidates, scored, sorted best first."""
        patterns_str = "\n".join(
            f"- {p}" for p in TITLE_PATTERNS
        )
        prompt = f"""You are a YouTube Shorts title strategist. Generate 10 title candidates for this video.

Video topic: {video.title}
Video script: {video.script[:500]}

Use these patterns as inspiration (adapt freely, don't copy literally):
{patterns_str}

For each title, score 0-10 on:
- curiosity: makes you NEED to click
- beginner_appeal: would a junior dev click?
- shareability: would you share this?
- clickability: does the title create a gap between what you know and want to know?

Bad titles (AVOID):
- "How freeCodeCamp Handles 100,000+ Lines of Code" (company-centric, no curiosity gap)
- "[Framework] [Version] Release Notes" (boring, no personal relevance)

Good titles (EMULATE):
- "Why Most Coding Projects Fail After 1,000 Lines"
- "The Mistake That Destroys Large Codebases"
- "The Hidden Cost of Copy-Paste Programming"
- "Why Senior Developers Never Use [X]"

Output JSON array sorted best first:
[{{"title": "...", "curiosity": 8, "beginner_appeal": 7, "shareability": 6, "clickability": 9}}]

No markdown. Valid JSON only."""

        response = self._ollama(prompt, temp=0.8, max_tokens=1024)
        items = self._extract_json_array(response)

        scored = []
        for item in items[:10]:
            title = (item.get("title") or "").strip()
            if not title or len(title) < 10:
                continue
            curiosity = min(10, max(0, int(item.get("curiosity", 5))))
            beginner = min(10, max(0, int(item.get("beginner_appeal", 5))))
            share = min(10, max(0, int(item.get("shareability", 5))))
            click = min(10, max(0, int(item.get("clickability", 5))))
            total = curiosity * 2 + beginner + share + click * 2

            scored.append({
                "title": title,
                "scores": {
                    "curiosity": curiosity,
                    "beginner_appeal": beginner,
                    "shareability": share,
                    "clickability": click,
                },
                "total": total,
            })

        scored.sort(key=lambda x: x["total"], reverse=True)
        logger.info("Generated %d scored titles, best='%s' (%.0f)", len(scored), scored[0]["title"] if scored else "(none)", scored[0]["total"] if scored else 0)
        return scored

    def _generate_captions(self, video: Video) -> list[dict]:
        """Generate 5 caption variants optimized for watch time + comments + follows."""
        prompt = f"""Write 5 YouTube Shorts caption variants for this video.

Topic: {video.title}
Script: {video.script[:500]}

GOALS:
- Increase watch time (tease value, create curiosity)
- Encourage comments (ask a question, invite opinion)
- Encourage follows (show authority, promise more value)

AVOID generic captions like "Follow for more coding tips."

Better: "What's the biggest coding project you've ever built?"
Better: "Most developers never learn this until it's too late."
Better: "I spent 3 years learning this. You can learn it in 3 minutes."

Output JSON array of EXACTLY 5 objects:
[{{"caption": "full caption text (2-3 sentences)", "watch_time_appeal": 8, "comment_appeal": 7, "follow_appeal": 6, "strategy": "curiosity_gap"}}]

No markdown. Valid JSON only."""

        response = self._ollama(prompt, temp=0.8, max_tokens=1024)
        items = self._extract_json_array(response)

        captions = []
        for item in items[:5]:
            caption = (item.get("caption") or "").strip()
            if not caption or len(caption) < 20:
                continue
            captions.append({
                "caption": caption,
                "watch_time_appeal": min(10, max(0, int(item.get("watch_time_appeal", 5)))),
                "comment_appeal": min(10, max(0, int(item.get("comment_appeal", 5)))),
                "follow_appeal": min(10, max(0, int(item.get("follow_appeal", 5)))),
                "strategy": item.get("strategy", ""),
            })

        logger.info("Generated %d caption variants", len(captions))
        return captions

    def _generate_hashtag_sets(self, video: Video) -> list[dict]:
        """Generate 3 hashtag sets: broad reach, coding niche, topic-specific."""
        prompt = f"""Generate 3 hashtag sets for this YouTube Shorts video.

Topic: {video.title}
Script: {video.script[:400]}

Set A - Broad reach (general tech, programming, education)
Set B - Coding niche (specific programming concepts)
Set C - Topic-specific (keywords from the video)

Maximum 5 hashtags per set. No hashtag stuffing.

Output JSON:
[{{"set": "A - Broad Reach", "hashtags": ["#coding", "#programming", "#tech", "#learntocode", "#shorts"]}}, ...]

No markdown. Valid JSON only."""

        response = self._ollama(prompt, temp=0.6, max_tokens=512)
        items = self._extract_json_array(response)

        sets = []
        for item in items[:3]:
            tags = item.get("hashtags", [])
            if isinstance(tags, list) and len(tags) > 0:
                clean_tags = [t.strip() for t in tags if t.strip().startswith("#")]
                if clean_tags:
                    sets.append({
                        "label": item.get("set", ""),
                        "hashtags": clean_tags[:5],
                    })

        logger.info("Generated %d hashtag sets", len(sets))
        return sets

    def _analyze_hook(self, video: Video) -> str:
        """Generate hook analysis."""
        prompt = f"""Analyze the hook strength for this YouTube Shorts.

Script: {video.script[:500]}

First 60 characters (the hook): {video.script[:60]}

Evaluate:
1. Does the first sentence create tension? (yes/no + why)
2. Does the second sentence increase curiosity? (yes/no + why)
3. Would a 16-year-old interested in coding stop scrolling? (yes/no + why)
4. One-sentence improvement suggestion.

Answer concisely, 3-4 sentences."""

        return self._ollama(prompt, temp=0.3, max_tokens=300)

    def _recommend_time(self) -> str:
        """Recommend best posting time based on general YouTube Shorts data."""
        prompt = """What is the best time to post a coding YouTube Short to maximize views and engagement?

Consider:
- Developers are active during lunch (12-1 PM) and after work (6-9 PM)
- Weekdays outperform weekends for educational content
- Shorts algorithm favors consistency over time

Give a specific recommendation (day + time) and one-sentence reason."""

        return self._ollama(prompt, temp=0.3, max_tokens=200)

    def _generate_why_performs(self, video: Video, best_title: str, best_caption: str) -> str:
        """Generate explanation of why this content should perform well."""
        prompt = f"""Explain why this YouTube Shorts video should perform well.

Topic: {video.title}
Best title: {best_title}
Best caption: {best_caption}
Hook: {video.script[:100]}

Write 2-3 sentences explaining:
- Who this appeals to
- Why they would watch to the end
- Why they would engage (like, comment, share)"""

        return self._ollama(prompt, temp=0.4, max_tokens=250)

    def generate(self, video: Video) -> dict:
        """Full publish pack generation pipeline."""
        logger.info("=" * 60)
        logger.info("Generating publish pack for: %s", video.title)
        logger.info("=" * 60)

        titles = self._generate_titles(video)
        captions = self._generate_captions(video)
        hashtag_sets = self._generate_hashtag_sets(video)
        hook_analysis = self._analyze_hook(video)
        best_time = self._recommend_time()

        best_title = titles[0]["title"] if titles else video.title
        best_caption = captions[0]["caption"] if captions else video.description
        why_performs = self._generate_why_performs(video, best_title, best_caption)

        pack = {
            "best_title": best_title,
            "all_titles": titles,
            "best_caption": best_caption,
            "all_captions": captions,
            "hashtag_sets": hashtag_sets,
            "hook_analysis": hook_analysis,
            "best_post_time": best_time,
            "why_performs": why_performs,
        }

        report_path = self._save_report(video, pack)
        logger.info("Publish pack saved: %s", report_path)
        return pack

    def _save_report(self, video: Video, pack: dict) -> Path:
        """Write publish pack to reports/video__publish_pack.txt."""
        report_dir = self.settings.reports_dir
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"video_{video.id or 'latest'}_publish_pack.txt"

        lines = []
        lines.append("=" * 60)
        lines.append("  CODE SHORTS BOT — PUBLISH PACK")
        lines.append(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
        lines.append(f"  Video ID: {video.id or 'latest'}")
        lines.append("=" * 60)
        lines.append("")
        lines.append(f"ORIGINAL TOPIC: {video.title}")
        lines.append(f"ORIGINAL DESCRIPTION: {video.description or ''}")
        lines.append(f"VIDEO PATH: {video.video_path or ''}")
        lines.append("")

        # Best Title
        lines.append("-" * 60)
        lines.append("BEST TITLE:")
        lines.append("-" * 60)
        lines.append(pack["best_title"])
        lines.append("")

        # All Titles
        lines.append("-" * 60)
        lines.append(f"ALL TITLES ({len(pack['all_titles'])}):")
        lines.append("-" * 60)
        for i, t in enumerate(pack["all_titles"], 1):
            scores = t["scores"]
            lines.append(
                f"  {i}. {t['title']}  "
                f"[curiosity={scores['curiosity']} "
                f"beginner={scores['beginner_appeal']} "
                f"share={scores['shareability']} "
                f"click={scores['clickability']} "
                f"total={t['total']}]"
            )
        lines.append("")

        # Best Caption
        lines.append("-" * 60)
        lines.append("BEST CAPTION:")
        lines.append("-" * 60)
        lines.append(pack["best_caption"])
        lines.append("")

        # All Captions
        lines.append("-" * 60)
        lines.append(f"ALL CAPTIONS ({len(pack['all_captions'])}):")
        lines.append("-" * 60)
        for i, c in enumerate(pack["all_captions"], 1):
            lines.append(
                f"  {i}. {c['caption']}  "
                f"[watch={c['watch_time_appeal']} "
                f"comment={c['comment_appeal']} "
                f"follow={c['follow_appeal']} "
                f"strategy={c['strategy']}]"
            )
        lines.append("")

        # Hashtag Sets
        lines.append("-" * 60)
        lines.append("HASHTAGS:")
        lines.append("-" * 60)
        for s in pack["hashtag_sets"]:
            lines.append(f"  {s['label']}:")
            lines.append(f"    {' '.join(s['hashtags'])}")
        lines.append("")

        # Hook Analysis
        lines.append("-" * 60)
        lines.append("HOOK ANALYSIS:")
        lines.append("-" * 60)
        lines.append(pack["hook_analysis"])
        lines.append("")

        # Best Post Time
        lines.append("-" * 60)
        lines.append("BEST POST TIME:")
        lines.append("-" * 60)
        lines.append(pack["best_post_time"])
        lines.append("")

        # Why This Should Perform
        lines.append("-" * 60)
        lines.append("WHY THIS SHOULD PERFORM:")
        lines.append("-" * 60)
        lines.append(pack["why_performs"])
        lines.append("")
        lines.append("=" * 60)
        lines.append("  PUBLISH PACK END")
        lines.append("=" * 60)
        lines.append("")

        content = "\n".join(lines) + "\n"
        report_path.write_text(content, encoding="utf-8")
        logger.info("Report written: %s", report_path)
        return report_path


