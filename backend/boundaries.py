"""Natural clip cut boundaries so moments end on a pause or a complete thought.

Whisper segments and AI-proposed timestamps often stop mid-thought, so every
clip creation, edit and render re-aligns its cut points to the nearest natural
boundary found in the transcript: a silence gap between spoken words, or the
end of a sentence. A cut is never moved far - the original timing wins when no
meaningful boundary is nearby.
"""

from __future__ import annotations

import re

PAUSE_GAP = 0.35          # seconds of silence that reads as a natural pause
KEEP_COST = 0.35          # cost of leaving the requested timestamp untouched
KIND_BONUS = {"sentence": 1.0, "pause": 0.75}
END_WINDOW = (2.0, 2.5)   # seconds an end cut may move (backwards, forwards)
START_WINDOW = (1.5, 2.0) # seconds a start cut may move (backwards, forwards)
SENTENCE_END = re.compile(r'[.!?…]["\')\]”’]?$')


def _segment_words(segment: dict) -> list[tuple[float, float, str]]:
    """Word spans for a segment; without word timestamps fall back to even tokens."""
    raw = segment.get("words") or []
    try:
        span_start, span_end = float(segment["start"]), float(segment["end"])
    except (KeyError, TypeError, ValueError):
        return []
    if raw:
        words: list[tuple[float, float, str]] = []
        for word in raw:
            try:
                start, end = float(word.get("start")), float(word.get("end", word.get("start")))
            except (TypeError, ValueError):
                continue
            if end > start:
                words.append((start, end, str(word.get("word", ""))))
        return words
    if span_end <= span_start:
        return []
    tokens = str(segment.get("text", "")).split()
    if not tokens:
        return [(span_start, span_end, "")]
    step = (span_end - span_start) / len(tokens)
    return [
        (span_start + index * step, span_start + (index + 1) * step, token)
        for index, token in enumerate(tokens)
    ]


def natural_boundaries(segments: list[dict]) -> tuple[list[tuple[float, str]], list[tuple[float, str]]]:
    """Cut candidates as (time, kind) pairs for clip starts and clip ends."""
    words: list[tuple[float, float, str]] = []
    for segment in segments:
        words.extend(_segment_words(segment))
    words.sort(key=lambda item: item[0])
    starts: list[tuple[float, str]] = []
    ends: list[tuple[float, str]] = []
    for index, (start, end, text) in enumerate(words):
        previous = words[index - 1] if index else None
        following = words[index + 1] if index + 1 < len(words) else None
        gap_before = start - previous[1] if previous else PAUSE_GAP
        gap_after = following[0] - end if following else PAUSE_GAP
        if gap_before >= PAUSE_GAP:
            starts.append((start, "pause"))
        if gap_after >= PAUSE_GAP:
            ends.append((end, "pause"))
        if text and SENTENCE_END.search(text.strip()):
            ends.append((end, "sentence"))
            if following is not None:
                starts.append((following[0], "sentence"))
    return _unique(starts), _unique(ends)


def _unique(points: list[tuple[float, str]]) -> list[tuple[float, str]]:
    kept: dict[float, str] = {}
    for time, kind in sorted(points):
        if kind == "sentence" or time not in kept:
            kept[time] = kind
    return sorted(kept.items())


def _pick(candidates: list[tuple[float, str]], time: float, back: float, forward: float) -> float:
    best, best_cost = time, KEEP_COST
    for when, kind in candidates:
        distance = when - time
        if distance < -back or distance > forward:
            continue
        cost = abs(distance) - KIND_BONUS.get(kind, 0.0)
        if cost < best_cost or (abs(cost - best_cost) <= 1e-9 and abs(distance) < abs(best - time)):
            best, best_cost = when, cost
    return best


def snap_boundaries(
    segments: list[dict],
    start: float,
    end: float,
    min_length: float = 3.0,
) -> tuple[float, float]:
    """Move a cut onto a pause or complete thought, never far from the request."""
    if end <= start:
        return start, end
    starts, ends = natural_boundaries(segments)
    snapped_start = _pick(starts, start, *START_WINDOW)
    snapped_end = _pick(ends, end, *END_WINDOW)
    if snapped_end - snapped_start < min_length:
        # Keep whichever original timing still leaves a usable clip.
        if end - snapped_start >= min_length:
            snapped_end = end
        elif snapped_end - start >= min_length:
            snapped_start = start
        else:
            snapped_start, snapped_end = start, end
    return snapped_start, snapped_end
