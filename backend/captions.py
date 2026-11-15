"""Synchronized ASS caption generation from faster-whisper word timestamps."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CAPTION_STYLES: dict[str, dict[str, Any]] = {
    "kinetic": {
        "label": "Kinetic", "font": "Arial", "font_size": 76, "primary": "#FFFFFF",
        "highlight": "#FFE066", "outline": 5, "shadow": 2, "background": "#000000",
        "background_opacity": 0, "max_words": 3, "position": "bottom", "uppercase": True,
        "word_highlight": True, "pop": True,
    },
    "word": {
        "label": "Word by word", "font": "Arial", "font_size": 86, "primary": "#FFFFFF",
        "highlight": "#7DE2FF", "outline": 6, "shadow": 2, "background": "#000000",
        "background_opacity": 0, "max_words": 1, "position": "middle", "uppercase": True,
        "word_highlight": True, "pop": True,
    },
    "cinematic": {
        "label": "Cinematic", "font": "Georgia", "font_size": 62, "primary": "#F5F0E6",
        "highlight": "#FFD9A0", "outline": 2, "shadow": 3, "background": "#000000",
        "background_opacity": 28, "max_words": 6, "position": "bottom", "uppercase": False,
        "word_highlight": False, "pop": False,
    },
    "podcast": {
        "label": "Bold podcast", "font": "Arial", "font_size": 80, "primary": "#FFFFFF",
        "highlight": "#FF6B6B", "outline": 6, "shadow": 1, "background": "#000000",
        "background_opacity": 0, "max_words": 4, "position": "bottom", "uppercase": False,
        "word_highlight": True, "pop": True,
    },
    "minimal": {
        "label": "Minimal subtitles", "font": "Arial", "font_size": 54, "primary": "#FFFFFF",
        "highlight": "#FFFFFF", "outline": 1, "shadow": 2, "background": "#000000",
        "background_opacity": 45, "max_words": 9, "position": "bottom", "uppercase": False,
        "word_highlight": False, "pop": False,
    },
}


@dataclass
class CaptionOptions:
    font: str = ""
    font_size: int = 0
    primary: str = ""
    highlight: str = ""
    outline: int = -1
    shadow: int = -1
    background: str = ""
    background_opacity: int = -1
    position: str = ""
    animation_speed: float = 1.0
    uppercase: bool | None = None
    max_words: int = 0
    margin_v: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def merged(self, style_name: str) -> dict[str, Any]:
        base = dict(CAPTION_STYLES.get(style_name, CAPTION_STYLES["kinetic"]))
        for key in ("font", "font_size", "primary", "highlight", "outline", "shadow", "background",
                    "background_opacity", "position", "uppercase", "max_words"):
            value = getattr(self, key)
            if value not in ("", -1, 0, None):
                base[key] = value
        if self.margin_v > 0:
            base["margin_v"] = self.margin_v
        if self.animation_speed > 0:
            base["animation_speed"] = self.animation_speed
        base.update(self.extra)
        return base


def _ass_color(hex_color: str, opacity: int = 100) -> str:
    cleaned = hex_color.strip().lstrip("#")
    if len(cleaned) == 3:
        cleaned = "".join(char * 2 for char in cleaned)
    if len(cleaned) != 6:
        cleaned = "FFFFFF"
    try:
        red, green, blue = int(cleaned[0:2], 16), int(cleaned[2:4], 16), int(cleaned[4:6], 16)
    except ValueError:
        red = green = blue = 255
    opacity = max(0, min(100, int(opacity)))
    alpha = int(round(255 * (1 - opacity / 100)))
    return f"&H{alpha:02X}{blue:02X}{green:02X}{red:02X}"


CAPTION_WIDTH = 1080
CAPTION_SIDE_MARGIN = 60
# Arial Bold averages ~0.74 em per glyph. Combined with smart wrapping this
# keeps captions inside the frame even when a line runs longer than expected.
GLYPH_WIDTH_RATIO = 0.74
MIN_FONT_SCALE = 0.62

# Latin fonts cannot render Devanagari conjuncts; Nirmala UI ships with
# Windows and is the reliable choice for Hindi / Hinglish captions.
DEVANAGARI = re.compile(r"[\u0900-\u097F]")
NIRMALA_UI = "Nirmala UI"


def _script_font(text: str, font: str) -> str:
    return NIRMALA_UI if DEVANAGARI.search(text) else font


def _fit_font_size(text: str, font_size: int) -> int:
    """Shrink a caption line so it fits the 1080 px frame instead of clipping."""
    available = max(120, CAPTION_WIDTH - CAPTION_SIDE_MARGIN * 2)
    estimated = max(1.0, len(text)) * font_size * GLYPH_WIDTH_RATIO
    if estimated <= available:
        return font_size
    scaled = int(font_size * available / estimated)
    return max(int(font_size * MIN_FONT_SCALE), min(font_size, scaled))


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        secs += 1
        centis = 0
    if secs >= 60:
        minutes += 1
        secs = 0
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _escape(text: str) -> str:
    text = text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\r", " ").replace("\n", " ")


def _pseudo_words(segments: list[dict]) -> list[dict]:
    words: list[dict] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        tokens = [token for token in text.split() if token]
        if not tokens:
            continue
        start = float(segment["start"])
        end = float(segment["end"])
        step = max(0.08, (end - start) / len(tokens))
        for index, token in enumerate(tokens):
            words.append({
                "start": start + index * step,
                "end": start + (index + 1) * step,
                "word": token,
            })
    return words


def _extract_words(content: dict, clip_start: float, clip_end: float) -> list[dict]:
    words: list[dict] = []
    for segment in content.get("segments", []):
        for word in segment.get("words") or []:
            start = float(word.get("start", 0))
            end = float(word.get("end", start))
            if end <= clip_start or start >= clip_end:
                continue
            text = str(word.get("word", "")).strip()
            if text:
                words.append({
                    "start": max(clip_start, start) - clip_start,
                    "end": min(clip_end, max(end, start + 0.05)) - clip_start,
                    "word": text,
                })
    if not words:
        pseudo = _pseudo_words(content.get("segments", []))
        for word in pseudo:
            if word["end"] > clip_start and word["start"] < clip_end:
                words.append({
                    "start": max(clip_start, word["start"]) - clip_start,
                    "end": min(clip_end, word["end"]) - clip_start,
                    "word": word["word"],
                })
    words.sort(key=lambda item: item["start"])
    return words


def _group_words(words: list[dict], max_words: int, max_chars: int, max_seconds: float) -> list[list[dict]]:
    cues: list[list[dict]] = []
    current: list[dict] = []
    for word in words:
        proposed = current + [word]
        text_length = len(" ".join(item["word"] for item in proposed))
        duration = proposed[-1]["end"] - proposed[0]["start"]
        boundary = bool(current) and re.search(r"[.!?]$", current[-1]["word"]) is not None and len(current) >= 2
        if current and (len(proposed) > max_words or text_length > max_chars or duration > max_seconds or boundary):
            cues.append(current)
            current = [word]
        else:
            current = proposed
    if current:
        cues.append(current)
    return cues


def _position_y(position: str, margin_v: int) -> int:
    if position == "top":
        return 250
    if position == "middle":
        return 960
    return 1920 - max(170, margin_v)


def build_ass(
    content: dict,
    clip_start: float,
    clip_end: float,
    style_name: str,
    options: CaptionOptions | None = None,
) -> str:
    """Return a complete ASS document synchronized to real transcript timestamps."""
    options = options or CaptionOptions()
    style = options.merged(style_name)
    words = _extract_words(content, clip_start, clip_end)
    if not words:
        return _empty_ass(style)

    max_chars = {"kinetic": 26, "word": 14, "cinematic": 52, "podcast": 34, "minimal": 60}.get(style_name, 34)
    cues = _group_words(words, int(style["max_words"]), max_chars, 4.6)
    primary = _ass_color(str(style["primary"]))
    highlight = _ass_color(str(style["highlight"]))
    background = _ass_color(str(style["background"]), int(style.get("background_opacity", 0)))
    y = _position_y(str(style.get("position", "bottom")), int(style.get("margin_v", 260)))
    font_size = int(style["font_size"])
    outline = max(0, int(style["outline"]))
    shadow = max(0, int(style["shadow"]))
    font = str(style["font"])
    uppercase = bool(style.get("uppercase"))
    speed = float(style.get("animation_speed", 1.0)) or 1.0
    word_highlight = bool(style.get("word_highlight"))
    pop = bool(style.get("pop"))
    border_style = 3 if int(style.get("background_opacity", 0)) > 0 else 1
    word_border = outline if border_style == 1 else max(2, outline)
    # Hindi / Hinglish captions need a Devanagari-capable font.
    sample = " ".join(word["word"] for word in words)
    if DEVANAGARI.search(sample):
        font = NIRMALA_UI
        uppercase = False

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Clip,{font},{font_size},{primary},{primary},&H00101010,{background},-1,0,0,0,100,100,0,0,{border_style},{word_border},{shadow},5,60,60,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    for cue_index, cue in enumerate(cues):
        cue_start = cue[0]["start"]
        cue_end = max(cue[-1]["end"], cue_start + 0.35)
        cue_text = " ".join(word["word"] for word in cue)
        min_duration = len(cue_text) / 17.0
        if cue_end - cue_start < min_duration:
            cue_end = min(words[-1]["end"], cue_start + min_duration)
        # Whisper word ends can trail the next word's start, so clamp each cue
        # to the next cue's start; otherwise two captions render on top of
        # each other and the glyphs overlap.
        if cue_index + 1 < len(cues):
            cue_end = min(cue_end, cues[cue_index + 1][0]["start"])
        cue_end = max(cue_end, cue_start + 0.05)
        if uppercase:
            rendered_words = [word["word"].upper() for word in cue]
        else:
            rendered_words = [word["word"] for word in cue]
        cue_font = _fit_font_size(" ".join(rendered_words), font_size)

        if word_highlight:
            previous_end = cue_start
            for index, word in enumerate(cue):
                event_start = cue_start if index == 0 else max(word["start"], previous_end)
                event_end = cue[index + 1]["start"] if index + 1 < len(cue) else cue_end
                event_end = min(event_end, cue_end)
                if event_end - event_start < 0.05:
                    event_end = event_start + 0.05
                previous_end = event_end
                parts = []
                for word_index, rendered in enumerate(rendered_words):
                    color = highlight if word_index == index else primary
                    parts.append(f"{{\\c{color}}}{_escape(rendered)}")
                animation = ""
                if pop:
                    animation = f"\\fscx86\\fscy86\\t(0,{int(90 * speed)},\\fscx100\\fscy100)"
                text = f"{{\\an5\\pos(540,{y})\\fs{cue_font}\\fad(35,35){animation}}}" + " ".join(parts)
                lines.append(f"Dialogue: 0,{_ass_time(event_start)},{_ass_time(event_end)},Clip,,0,0,0,,{text}")
        else:
            animation = ""
            if style_name == "cinematic":
                animation = f"\\fad(180,180)\\fscx100\\fscy100\\t(0,{int(260 * speed)},\\fsp2)"
            else:
                animation = "\\fad(80,60)"
            text = f"{{\\an5\\pos(540,{y})\\fs{cue_font}{animation}}}" + _escape(" ".join(rendered_words))
            lines.append(f"Dialogue: 0,{_ass_time(cue_start)},{_ass_time(cue_end)},Clip,,0,0,0,,{text}")

    return "\n".join(lines) + "\n"


def _empty_ass(style: dict[str, Any]) -> str:
    return "\n".join([
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1080", "PlayResY: 1920", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Clip,{style['font']},{style['font_size']},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00000000,-1,0,0,0,100,100,0,0,1,4,2,5,60,60,0,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]) + "\n"


def write_ass(
    path: Path,
    content: dict,
    clip_start: float,
    clip_end: float,
    style_name: str,
    options: CaptionOptions | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(content, clip_start, clip_end, style_name, options), encoding="utf-8")
    return path


def clip_srt(content: dict, clip_start: float, clip_end: float) -> str:
    """Render a clip-relative SRT using real transcript segments."""
    entries: list[str] = []
    index = 1
    for segment in content.get("segments", []):
        start = float(segment.get("start", 0))
        end = float(segment.get("end", start))
        if end <= clip_start or start >= clip_end:
            continue
        local_start = max(0.0, start - clip_start)
        local_end = min(clip_end - clip_start, max(local_start + 0.2, end - clip_start))
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        entries.append(
            f"{index}\n{_srt_time(local_start)} --> {_srt_time(local_end)}\n{text}\n"
        )
        index += 1
    return "\n".join(entries)


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, int(round(seconds * 1000)))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{millis:03}"


def caption_options_from_json(raw: str | None) -> CaptionOptions:
    if not raw:
        return CaptionOptions()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return CaptionOptions()
    if not isinstance(data, dict):
        return CaptionOptions()
    known = {field_name for field_name in CaptionOptions.__dataclass_fields__ if field_name != "extra"}
    values = {key: data[key] for key in known if key in data}
    extra = {key: value for key, value in data.items() if key not in known}
    return CaptionOptions(**values, extra=extra)
