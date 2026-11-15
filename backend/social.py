"""Viral social caption writing for rendered clips.

The writer runs entirely on the local Ollama model and is grounded in the
clip's own transcript excerpt: it may sharpen the hook, never invent facts.
When Ollama is unavailable a deterministic local writer keeps the feature
usable. Nothing is published anywhere - captions are text the creator copies.
"""

from __future__ import annotations

import json
import re
from typing import Any

from backend.ollama import OllamaError, chat_json

SOCIAL_SYSTEM = (
    "You write scroll-stopping captions for short-form podcast clips. Every claim must be "
    "supported by the supplied transcript excerpt. You may sharpen the framing, never invent "
    "facts, numbers or quotes. Return strict JSON only."
)

VARIANT_STYLES: list[dict[str, str]] = [
    {
        "id": "curiosity",
        "label": "Curiosity gap",
        "brief": "Open with a question that makes the viewer need the answer. Keep it honest.",
    },
    {
        "id": "contrarian",
        "label": "Contrarian take",
        "brief": "State the surprising or counter-intuitive idea from the clip, then justify it.",
    },
    {
        "id": "stat",
        "label": "Number shock",
        "brief": "Lead with the most surprising number in the clip and explain why it matters.",
    },
    {
        "id": "story",
        "label": "Story opener",
        "brief": "Open like a story: what happened, then the lesson. Keep the stakes clear.",
    },
    {
        "id": "cta",
        "label": "Save & share",
        "brief": "Lead with the takeaway, then invite saves, shares or a follow for more.",
    },
]

PROMPT = """Write {count} social captions for this podcast clip.

Return JSON exactly in this shape:
{{"variants":[{{"style":"{first_style}","hook":"Under 90 characters","caption":"Two to four short sentences","hashtags":["#Shorts","#Podcast"],"cta":"One line"}}]}}

Transcript excerpt:
{excerpt}

Clip working title: {title}
Clip length in seconds: {duration}

Rules:
- Exactly {count} variants, one per style, in this order: {styles}.
- The hook is the first line a viewer reads; make it specific to this clip.
- The caption must stay accurate to the excerpt; do not add facts or quotes.
- Use short lines. No emoji spam, at most one emoji per caption.
- 5 to 8 hashtags, each starting with #. Mix one broad tag, one niche topic tag
  and the rest with what the excerpt is genuinely about.
- The CTA invites a save, share or follow, never a purchase.
"""

HASHTAG_POOL = [
    "#Shorts", "#Podcast", "#Podcasting", "#Business", "#Entrepreneurship", "#Marketing",
    "#ContentCreation", "#CreatorEconomy", "#Growth", "#Startup", "#Money", "#Mindset",
    "#RajShamani", "#FiguringOut", "#LearnOnYouTube",
]


def _clean(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _clean_tags(tags: Any) -> list[str]:
    if isinstance(tags, str):
        tags = re.split(r"[\s,]+", tags)
    cleaned: list[str] = []
    if isinstance(tags, list):
        for tag in tags:
            text = _clean(tag, 40)
            if not text:
                continue
            text = "#" + re.sub(r"[^A-Za-z0-9_]", "", text.lstrip("#"))
            if len(text) > 1 and text not in cleaned:
                cleaned.append(text)
    return cleaned[:8]


def _fallback(clip: dict[str, Any], excerpt: str) -> list[dict[str, Any]]:
    """Deterministic writer used when the local model is unavailable."""
    title = _clean(clip.get("title"), 90) or "this podcast moment"
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", excerpt) if part.strip()]
    lead = sentences[0] if sentences else title
    numbers = re.findall(r"\$[\d,.]+\s?(?:million|billion|thousand)?|\b\d[\d,]*%|\b\d[\d,]*(?:\.\d+)?\s"
                         r"(?:million|billion|thousand|creators|stores|customers|people|months)", lead, re.I)
    stat = numbers[0].strip() if numbers else ""
    variants = [
        {
            "style": "curiosity",
            "label": "Curiosity gap",
            "hook": f"Why {title.lower()} actually works",
            "caption": f"{lead} Here is what that looks like in practice.",
            "hashtags": ["#Shorts", "#Podcast", "#Business", "#ContentCreation"],
            "cta": "Save this one and share it with a founder you know.",
        },
        {
            "style": "contrarian",
            "label": "Contrarian take",
            "hook": f"The {title.lower()} take nobody expects",
            "caption": f"{lead} Most people get this part wrong.",
            "hashtags": ["#Shorts", "#Entrepreneurship", "#Marketing", "#Podcasting"],
            "cta": "Follow for more clips like this.",
        },
        {
            "style": "stat",
            "label": "Number shock",
            "hook": f"{stat or 'This number'} changed the whole game".strip(),
            "caption": f"{lead} The numbers do not lie.",
            "hashtags": ["#Shorts", "#Business", "#Money", "#Growth"],
            "cta": "Save it before you forget.",
        },
        {
            "style": "story",
            "label": "Story opener",
            "hook": f"It started with one decision: {title.lower()}",
            "caption": f"{lead} Then the rest followed.",
            "hashtags": ["#Shorts", "#Podcast", "#CreatorEconomy", "#Startup"],
            "cta": "Share this with someone building something.",
        },
        {
            "style": "cta",
            "label": "Save & share",
            "hook": f"{title} - worth 30 seconds of your scroll",
            "caption": f"{lead} Steal this one.",
            "hashtags": ["#Shorts", "#Podcast", "#Mindset", "#RajShamani"],
            "cta": "Save it, share it, and follow for the next clip.",
        },
    ]
    return variants


def _validate(raw: Any, clip: dict[str, Any], excerpt: str) -> list[dict[str, Any]] | None:
    if not isinstance(raw, dict):
        return None
    items = raw.get("variants")
    if not isinstance(items, list) or not items:
        return None
    cleaned: list[dict[str, Any]] = []
    for index, item in enumerate(items[: len(VARIANT_STYLES)]):
        if not isinstance(item, dict):
            return None
        hook = _clean(item.get("hook"), 120)
        caption = _clean(item.get("caption"), 600)
        if not hook or not caption:
            return None
        style = VARIANT_STYLES[index]
        tags = _clean_tags(item.get("hashtags"))
        if len(tags) < 3:
            tags = (tags + [tag for tag in HASHTAG_POOL if tag not in tags])[:8]
        cleaned.append({
            "style": style["id"],
            "label": style["label"],
            "hook": hook,
            "caption": caption,
            "hashtags": tags,
            "cta": _clean(item.get("cta"), 120) or "Save this and share it with a friend.",
        })
    if len(cleaned) < 2:
        return None
    return cleaned


def write_viral_captions(
    clip: dict[str, Any],
    excerpt: str,
    model: str,
    count: int = 5,
) -> dict[str, Any]:
    """Return trending caption variants for a clip, grounded in its transcript."""
    count = max(2, min(int(count or 5), len(VARIANT_STYLES)))
    styles = VARIANT_STYLES[:count]
    prompt = PROMPT.format(
        count=count,
        styles=", ".join(item["id"] for item in styles),
        first_style=styles[0]["id"],
        excerpt=_clean(excerpt, 1800),
        title=_clean(clip.get("title"), 100),
        duration=int(clip.get("duration") or 0),
    )
    try:
        raw = chat_json(model, prompt, system=SOCIAL_SYSTEM, num_predict=1400, temperature=0.6)
        validated = _validate(raw, clip, excerpt)
        if validated:
            return {"variants": validated, "generated_by": "ollama", "model": model}
    except OllamaError:
        pass
    return {"variants": _fallback(clip, excerpt), "generated_by": "local-fallback", "model": model}


def captions_to_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)
