"""Opt-in dead-air removal inside a clip.

Long silences (question breaks, b-roll cutaways, thinking pauses) make Shorts
feel slow. When the creator opts in, gaps longer than ~1.2 s are cut down to a
natural ~0.35 s pause with FFmpeg trim/concat, and the transcript is re-timed
onto the trimmed timeline so burned-in captions stay word-synchronised.
"""

from __future__ import annotations

from typing import Any

GAP_THRESHOLD = 1.2   # seconds of silence treated as dead air
PAUSE_KEEP = 0.35     # seconds of pause kept where the speech resumes
EDGE_GUARD = 0.05     # padding before the next word so nothing is clipped


def _clip_words(content: dict, start: float, end: float) -> list[dict]:
    words: list[dict] = []
    for segment in content.get("segments", []):
        raw = segment.get("words") or []
        seg_start, seg_end = float(segment.get("start", 0)), float(segment.get("end", 0))
        if raw:
            for word in raw:
                w_start, w_end = float(word.get("start", 0)), float(word.get("end", word.get("start", 0)))
                if w_end > start and w_start < end:
                    text = str(word.get("word", "")).strip()
                    if text:
                        words.append({"start": w_start, "end": w_end, "word": text})
        elif seg_end > start and seg_start < end and str(segment.get("text", "")).strip():
            tokens = str(segment.get("text", "")).split()
            if seg_end <= seg_start or not tokens:
                continue
            step = (seg_end - seg_start) / len(tokens)
            for index, token in enumerate(tokens):
                w_start, w_end = seg_start + index * step, seg_start + (index + 1) * step
                if w_end > start and w_start < end:
                    words.append({"start": w_start, "end": w_end, "word": token})
    words.sort(key=lambda item: item["start"])
    return words


def plan_trim(
    content: dict, start: float, end: float,
    threshold: float = GAP_THRESHOLD, keep: float = PAUSE_KEEP,
) -> dict[str, Any] | None:
    """Return pieces, a re-timed transcript and the trimmed duration.

    ``None`` when there is nothing worth cutting (single piece, no long gaps,
    or a degenerate clip).
    """
    if end - start < 3.0:
        return None
    words = [word for word in _clip_words(content, start, end) if word["end"] > word["start"]]
    if len(words) < 2:
        return None
    pieces: list[list[float]] = [[start, end]]
    for previous, following in zip(words, words[1:]):
        gap = following["start"] - previous["end"]
        if gap <= threshold:
            continue
        cut_start = previous["end"] + keep
        cut_end = max(cut_start + 0.01, following["start"] - EDGE_GUARD)
        if cut_end <= start or cut_start >= end:
            continue
        last = pieces[-1]
        last[1] = max(last[0] + 0.5, min(cut_start, last[1]))
        if cut_end < end:
            pieces.append([cut_end, end])
    pieces = [[s, e] for s, e in pieces if e - s >= 0.5]
    if len(pieces) < 2:
        return None
    duration = sum(piece_end - piece_start for piece_start, piece_end in pieces)
    if duration < 3.0:
        return None
    return {"pieces": pieces, "content": _remap_content(content, pieces), "duration": duration}


def _remap_content(content: dict, pieces: list[list[float]]) -> dict:
    """Re-time the transcript onto the trimmed (output) timeline."""
    remapped: list[dict] = []
    offset = 0.0
    for piece_start, piece_end in pieces:
        for segment in content.get("segments", []):
            seg_start, seg_end = float(segment.get("start", 0)), float(segment.get("end", 0))
            overlap_start = max(seg_start, piece_start)
            overlap_end = min(seg_end, piece_end)
            if overlap_end - overlap_start <= 0.05:
                continue
            new_segment: dict[str, Any] = {
                "start": round(offset + (overlap_start - piece_start), 3),
                "end": round(offset + (overlap_end - piece_start), 3),
                "text": str(segment.get("text", "")).strip(),
            }
            words = []
            for word in segment.get("words") or []:
                w_start, w_end = float(word.get("start", 0)), float(word.get("end", word.get("start", 0)))
                if w_end <= piece_start or w_start >= piece_end:
                    continue
                w_start, w_end = max(w_start, piece_start), min(w_end, piece_end)
                if w_end - w_start <= 0:
                    continue
                words.append({
                    "start": round(offset + (w_start - piece_start), 3),
                    "end": round(offset + (w_end - piece_start), 3),
                    "word": str(word.get("word", "")),
                })
            if words:
                new_segment["words"] = words
                new_segment["text"] = " ".join(word["word"] for word in words)
            remapped.append(new_segment)
        offset += piece_end - piece_start
    return {"segments": remapped, "duration_seconds": round(offset, 3)}


def concat_filters(pieces: list[list[float]], stream: str, kind: str) -> tuple[str, str]:
    """FFmpeg trim/concat filter chain for one stream.

    Returns the filter fragment and the label that holds the concatenated
    output (``[vc]`` for video, ``[ac]`` for audio).
    """
    label = "vc" if kind == "video" else "ac"
    trim = "trim" if kind == "video" else "atrim"
    segments: list[str] = []
    for index, (piece_start, piece_end) in enumerate(pieces):
        segments.append(
            f"[{stream}]{trim}=start={piece_start:.3f}:end={piece_end:.3f},"
            f"{'setpts' if kind == 'video' else 'asetpts'}=PTS-STARTPTS[{label}{index}]"
        )
    count = len(pieces)
    if count == 1:
        return segments[0].split(f"[{label}0]")[0], f"[{label}0]"
    concat = "".join(f"[{label}{index}]" for index in range(count))
    segments.append(f"{concat}concat=n={count}:v={1 if kind == 'video' else 0}:a={0 if kind == 'video' else 1}[{label}]")
    return ";".join(segments), f"[{label}]"
