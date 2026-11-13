"""Grounded clip metadata generation with a deterministic local fallback."""

from __future__ import annotations

import json
import re
from typing import Any

from backend.ollama import OllamaError, chat_json

METADATA_SYSTEM = (
    "You write accurate metadata for short-form podcast clips. You only describe what is "
    "present in the supplied transcript excerpt. Never invent facts or clickbait claims. Return strict JSON."
)

PROMPT = """Create metadata for this short-form clip.

Return JSON exactly in this shape:
{{"title":"Specific title under 80 characters","description":"One or two accurate sentences about the clip","hashtags":["#Shorts","#Podcast","#Topic"],"category":"Motivational","thumbnail_text":"3 to 6 punchy words"}}

Transcript excerpt:
{excerpt}

Existing working title: {title}
Detected category: {category}

Rules:
- The description must accurately reflect the excerpt.
- 3 to 8 hashtags, each starting with #.
- thumbnail_text must be 3 to 6 words, no hashtags.
- Do not claim anything that is not in the excerpt.
"""


def _clean(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _fallback(clip: dict[str, Any], excerpt: str) -> dict[str, Any]:
    title = _clean(clip.get("title"), 80) or _clean(excerpt.split(".")[0], 80) or "Podcast clip"
    category = _clean(clip.get("category"), 40) or "Insight"
    category_tag = re.sub(r"[^A-Za-z0-9]", "", category)
    hashtags = ["#Shorts", "#Podcast", "#" + (category_tag or "Clip")]
    clean_tags: list[str] = []
    for tag in hashtags:
        if tag != "#" and tag not in clean_tags:
            clean_tags.append(tag)
    words = [word.strip(".,!?;:\"'") for word in title.split() if word.strip(".,!?;:\"'")]
    thumbnail = " ".join(words[:5]).upper() or "PODCAST CLIP"
    return {
        "title": title,
        "description": _clean(excerpt, 300) or "A short clip from this podcast conversation.",
        "hashtags": clean_tags,
        "category": category,
        "thumbnail_text": thumbnail,
        "generated_by": "local-fallback",
    }


def _validate(raw: Any, clip: dict[str, Any], excerpt: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    title = _clean(raw.get("title"), 80)
    description = _clean(raw.get("description"), 320)
    if not title or not description:
        return None
    tags = raw.get("hashtags")
    if isinstance(tags, str):
        tags = tags.split()
    clean_tags: list[str] = []
    if isinstance(tags, list):
        for tag in tags:
            text = _clean(tag, 40)
            if not text:
                continue
            text = "#" + re.sub(r"[^A-Za-z0-9_]", "", text.lstrip("#"))
            if len(text) > 1 and text not in clean_tags:
                clean_tags.append(text)
    if len(clean_tags) < 2:
        clean_tags = ["#Shorts", "#Podcast"]
    thumbnail = _clean(raw.get("thumbnail_text"), 48) or " ".join(title.split()[:5]).upper()
    return {
        "title": title,
        "description": description,
        "hashtags": clean_tags[:8],
        "category": _clean(raw.get("category"), 40) or _clean(clip.get("category"), 40) or "Insight",
        "thumbnail_text": thumbnail,
        "generated_by": "ollama",
    }


def generate_clip_metadata(
    clip: dict[str, Any],
    excerpt: str,
    model: str,
) -> dict[str, Any]:
    """Generate metadata locally; never blocks the whole render when Ollama is offline."""
    prompt = PROMPT.format(
        excerpt=_clean(excerpt, 1800),
        title=_clean(clip.get("title"), 100),
        category=_clean(clip.get("category"), 40),
    )
    try:
        raw = chat_json(model, prompt, system=METADATA_SYSTEM, num_predict=500, temperature=0.3)
        validated = _validate(raw, clip, excerpt)
        if validated:
            return validated
    except OllamaError:
        pass
    return _fallback(clip, excerpt)


def metadata_to_json(metadata: dict[str, Any]) -> str:
    return json.dumps(metadata, ensure_ascii=False, indent=2)
