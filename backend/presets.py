"""Editing presets for vertical clip rendering."""

from __future__ import annotations

PRESETS: dict[str, dict] = {
    "cinematic": {
        "label": "Cinematic",
        "description": "Slow face-aware framing, warm contrast, elegant captions.",
        "zoom": 1.06,
        "color_filter": "eq=contrast=1.06:saturation=0.92:brightness=-0.01",
        "caption_style": "cinematic",
        "fps": 30,
    },
    "podcast": {
        "label": "Podcast",
        "description": "Clean centre framing with bold, readable podcast captions.",
        "zoom": 1.0,
        "color_filter": "",
        "caption_style": "podcast",
        "fps": 30,
    },
    "minimal": {
        "label": "Minimal",
        "description": "Natural colour and understated subtitles.",
        "zoom": 1.0,
        "color_filter": "",
        "caption_style": "minimal",
        "fps": 30,
    },
    "gaming": {
        "label": "Gaming",
        "description": "Punchy saturation and fast kinetic captions.",
        "zoom": 1.04,
        "color_filter": "eq=contrast=1.08:saturation=1.18",
        "caption_style": "kinetic",
        "fps": 30,
    },
    "motivational": {
        "label": "Motivational",
        "description": "Warm, high-contrast framing with kinetic emphasis.",
        "zoom": 1.03,
        "color_filter": "eq=contrast=1.05:saturation=1.06:brightness=0.01",
        "caption_style": "kinetic",
        "fps": 30,
    },
}


def preset(name: str) -> dict:
    return PRESETS.get(name, PRESETS["podcast"])
