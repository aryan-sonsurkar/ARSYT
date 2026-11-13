"""Bounded Ollama highlight detection, validation, ranking and candidate review."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from backend.config import settings
from backend.database import connect, now_iso, row, rows
from backend.ingestion import IngestionError, JobCancelled
from backend.jobs import IngestionJobManager
from backend.ollama import OllamaError, available_models, chat_json

CATEGORIES = [
    "Motivational", "Story", "Opinion", "Advice", "Education", "Surprising",
    "Funny", "Debate", "Emotional", "Insight", "Interview",
]
CATEGORY_LOOKUP = {category.lower(): category for category in CATEGORIES}
SCORE_WEIGHTS = {"hook": 0.30, "emotion": 0.15, "clarity": 0.15, "context": 0.15, "value": 0.25}

HIGHLIGHT_SYSTEM = (
    "You are the clip editor for a podcast. You only select moments that exist in the supplied "
    "transcript. You never invent quotes, facts or timestamps. You return strict JSON."
)

PROMPT_TEMPLATE = """Select the most engaging short-form video moments from this transcript chunk.

Return JSON exactly in this shape:
{{"candidates":[{{"start_time":12.5,"end_time":48.0,"title":"Short specific title","hook":"A short grounded opening line","reason":"Why this moment works as a standalone short","category":"Motivational","scores":{{"hook":85,"emotion":70,"clarity":90,"context":80,"value":75}},"overall_score":82}}]}}

Rules:
- Return at most {limit} candidates. Quality matters more than quantity.
- Each clip must be {min_duration}-{max_duration} seconds.
- start_time and end_time must be copied from the timestamped transcript lines below.
- Choose complete thoughts with a strong hook and a clean ending.
- The hook must use wording grounded in the transcript, not a fabricated quote.
- Categories: {categories}.
- Scores are integers 0-100 for hook, emotion, clarity, context and value.
- Only select a moment when the transcript contains enough content for that duration.

Transcript chunk ({chunk_start:.1f}s to {chunk_end:.1f}s):
{lines}
"""


@dataclass(frozen=True)
class HighlightRequest:
    model: str
    count: int
    min_duration: float
    max_duration: float


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number == number else default


def _clean_text(value: Any, limit: int = 400) -> str:
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def _excerpt_for(segments: list[dict], start: float, end: float, limit: int = 700) -> str:
    texts = [
        str(segment.get("text", "")).strip()
        for segment in segments
        if segment["end"] > start + 0.01 and segment["start"] < end - 0.01
    ]
    return _clean_text(" ".join(text for text in texts if text), limit)


def _snap_to_transcript(segments: list[dict], segment_start: float, segment_end: float) -> tuple[float, float] | None:
    overlapping = [
        segment for segment in segments
        if segment["end"] > segment_start + 0.01 and segment["start"] < segment_end - 0.01
    ]
    if not overlapping:
        return None
    return float(overlapping[0]["start"]), float(overlapping[-1]["end"])


def _grounded_hook(hook: str, excerpt: str) -> str:
    hook = _clean_text(hook, 240)
    if not hook:
        return _first_sentence(excerpt)
    if not excerpt:
        return hook
    hook_words = set(re.findall(r"[a-z0-9']+", hook.lower()))
    excerpt_words = set(re.findall(r"[a-z0-9']+", excerpt.lower()))
    if not hook_words:
        return _first_sentence(excerpt)
    overlap = len(hook_words & excerpt_words) / max(1, len(hook_words))
    return hook if overlap >= 0.55 else _first_sentence(excerpt)


def _first_sentence(text: str, limit: int = 150) -> str:
    cleaned = _clean_text(text, limit)
    match = re.search(r"^(.{20,}?[.!?])(?:\s|$)", cleaned)
    return (match.group(1) if match else cleaned)[:limit]


def _category(value: Any, excerpt: str) -> str:
    key = _clean_text(value, 40).lower()
    if key in CATEGORY_LOOKUP:
        return CATEGORY_LOOKUP[key]
    for category, direct in CATEGORY_LOOKUP.items():
        if category in key:
            return direct
    lower = excerpt.lower()
    if any(word in lower for word in ("funny", "laugh", "joke")):
        return "Funny"
    if any(word in lower for word in ("story", "when i was", "one day")):
        return "Story"
    if any(word in lower for word in ("should", "best way", "advice")):
        return "Advice"
    if any(word in lower for word in ("research", "study", "science", "learn")):
        return "Education"
    return "Insight"


def _score_candidate(raw: dict, segments: list[dict], request: HighlightRequest, duration: float) -> dict | None:
    start = _as_float(raw.get("start_time"), -1)
    end = _as_float(raw.get("end_time"), -1)
    if start < 0 or end <= start:
        return None
    snapped = _snap_to_transcript(segments, _clamp(start, 0, duration), _clamp(end, 0, duration))
    if not snapped:
        return None
    clipped_start, clipped_end = snapped
    excerpt = _excerpt_for(segments, clipped_start, clipped_end)
    if not excerpt:
        return None
    length = clipped_end - clipped_start
    if length < max(12.0, request.min_duration * 0.55):
        return None
    if length > request.max_duration * 1.35:
        clipped_end = clipped_start + request.max_duration * 1.35
        snapped = _snap_to_transcript(segments, clipped_start, clipped_end)
        if not snapped:
            return None
        clipped_start, clipped_end = snapped
        excerpt = _excerpt_for(segments, clipped_start, clipped_end)
        length = clipped_end - clipped_start
    if length > request.max_duration * 1.5:
        return None

    raw_scores = raw.get("scores") if isinstance(raw.get("scores"), dict) else {}
    scores = {
        key: int(round(_clamp(_as_float(raw_scores.get(key), 55), 0, 100)))
        for key in SCORE_WEIGHTS
    }
    passed_scores = int(round(_clamp(_as_float(raw.get("overall_score"), sum(scores.values()) / 5), 0, 100)))
    computed = int(round(sum(scores[key] * weight for key, weight in SCORE_WEIGHTS.items())))
    overall = int(round(computed * 0.75 + passed_scores * 0.25))
    if overall < 45:
        return None

    title = _clean_text(raw.get("title"), 120) or _first_sentence(excerpt, 80)
    return {
        "start_time": round(clipped_start, 3),
        "end_time": round(clipped_end, 3),
        "title": title,
        "hook": _grounded_hook(str(raw.get("hook", "")), excerpt),
        "reason": _clean_text(raw.get("reason"), 500) or "This is a complete, standalone moment from the conversation.",
        "category": _category(raw.get("category"), excerpt),
        "scores": scores,
        "overall_score": overall,
        "excerpt": excerpt,
    }


def _overlap_ratio(first: dict, second: dict) -> float:
    first_length = max(0.001, first["end_time"] - first["start_time"])
    second_length = max(0.001, second["end_time"] - second["start_time"])
    overlap = max(0.0, min(first["end_time"], second["end_time"]) - max(first["start_time"], second["start_time"]))
    return overlap / max(0.001, min(first_length, second_length))


def _dedupe(candidates: list[dict]) -> list[dict]:
    ranked = sorted(candidates, key=lambda item: item["overall_score"], reverse=True)
    kept: list[dict] = []
    for candidate in ranked:
        duplicate = False
        for existing in kept:
            if _overlap_ratio(candidate, existing) > 0.55:
                duplicate = True
                break
            title_ratio = SequenceMatcher(
                None, candidate["title"].lower(), existing["title"].lower()
            ).ratio()
            excerpt_ratio = SequenceMatcher(
                None, candidate["excerpt"][:400].lower(), existing["excerpt"][:400].lower()
            ).ratio()
            if title_ratio > 0.86 and excerpt_ratio > 0.72:
                duplicate = True
                break
        if not duplicate:
            kept.append(candidate)
    return kept


def _select_best(candidates: list[dict], count: int, max_duration: float) -> list[dict]:
    selected: list[dict] = []
    for candidate in sorted(candidates, key=lambda item: item["overall_score"], reverse=True):
        if len(selected) >= count:
            break
        if any(
            _overlap_ratio(candidate, existing) > 0.22
            or abs(candidate["overall_score"] - existing["overall_score"]) < 2
            and _overlap_ratio(candidate, existing) > 0.05
            for existing in selected
        ):
            continue
        if candidate["end_time"] - candidate["start_time"] > max_duration * 1.4:
            continue
        selected.append(candidate)
    return selected


def _chunk_segments(segments: list[dict], max_seconds: float = 150.0, max_chars: int = 4800) -> list[dict]:
    chunks: list[dict] = []
    index = 0
    while index < len(segments):
        start_index = index
        chars = 0
        chunk_start = float(segments[index]["start"])
        chunk_end = chunk_start
        while index < len(segments):
            segment = segments[index]
            addition = len(str(segment.get("text", ""))) + 24
            if index > start_index and (chars + addition > max_chars or float(segment["end"]) - chunk_start > max_seconds):
                break
            chars += addition
            chunk_end = float(segment["end"])
            index += 1
        chunks.append({"start": chunk_start, "end": chunk_end, "segments": segments[start_index:index]})
        if index >= len(segments):
            break
        # Keep twenty seconds of context overlap so a moment is not cut between chunks.
        overlap_start = max(chunk_start, chunk_end - 20.0)
        next_index = start_index
        while next_index < index and float(segments[next_index]["end"]) <= overlap_start:
            next_index += 1
        index = max(index - 1, next_index)
        if index <= start_index:
            index = start_index + 1
    return chunks


def _render_chunk(chunk: dict) -> str:
    lines = []
    for segment in chunk["segments"]:
        lines.append(f"[{float(segment['start']):.1f}-{float(segment['end']):.1f}] {str(segment.get('text', '')).strip()}")
    return "\n".join(lines)


class HighlightService:
    def __init__(self, jobs: IngestionJobManager) -> None:
        self.jobs = jobs

    def get_candidates(self, project_id: str) -> list[dict]:
        items = rows(
            "SELECT * FROM candidates WHERE project_id=? ORDER BY selected DESC, overall_score DESC, start_time",
            (project_id,),
        )
        for item in items:
            item["scores"] = json.loads(item.pop("scores_json", "{}") or "{}")
            item["selected"] = bool(item.get("selected"))
        return items

    def _transcript_segments(self, project_id: str) -> dict:
        transcript = row("SELECT * FROM transcripts WHERE project_id=?", (project_id,))
        if not transcript:
            raise IngestionError("Create a completed transcript before detecting highlights.")
        path = Path(transcript["json_path"]).resolve()
        expected = (settings.projects_dir / project_id / "transcript").resolve()
        if expected not in path.parents or not path.is_file():
            raise IngestionError("The saved transcript file is unavailable.")
        try:
            content = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IngestionError("The saved transcript cannot be read.") from exc
        segments = content.get("segments") or []
        if not segments:
            raise IngestionError("The transcript has no timestamped segments to analyse.")
        return content

    async def start(self, project_id: str, request: HighlightRequest) -> dict:
        project = row("SELECT * FROM projects WHERE id=?", (project_id,))
        if not project:
            raise IngestionError("Project not found.")
        self._transcript_segments(project_id)
        installed = available_models()
        if request.model not in installed:
            raise IngestionError(
                f"Ollama model '{request.model}' is not installed. Installed models: {', '.join(installed) or 'none'}. "
                "Choose an installed model in Settings; ClipFree never downloads models automatically."
            )
        active = row(
            "SELECT id FROM jobs WHERE project_id=? AND kind='highlights' AND state IN ('queued','running') LIMIT 1",
            (project_id,),
        )
        if active:
            raise IngestionError("Highlight detection is already active for this project.")
        job = self.jobs.create(project_id, "highlights")
        payload = {
            "model": request.model, "count": request.count,
            "min_duration": request.min_duration, "max_duration": request.max_duration,
        }
        self.jobs.update(job["id"], payload_json=json.dumps(payload))
        self.jobs.register_task(self.run(project_id, job["id"], request))
        return self.jobs.get(job["id"])

    async def retry(self, job_id: str) -> dict:
        previous = self.jobs.get(job_id)
        try:
            payload = json.loads(previous.get("payload_json") or "{}")
            request = HighlightRequest(**payload)
        except (TypeError, ValueError) as exc:
            raise IngestionError("The previous highlight settings could not be recovered.") from exc
        return await self.start(previous["project_id"], request)

    async def run(self, project_id: str, job_id: str, request: HighlightRequest) -> None:
        started = time.perf_counter()
        try:
            content = self._transcript_segments(project_id)
            segments = [segment for segment in content["segments"] if str(segment.get("text", "")).strip()]
            duration = float(content.get("duration_seconds") or (segments[-1]["end"] if segments else 0))
            chunks = _chunk_segments(segments)
            self.jobs.update(job_id, state="running", stage=f"analysing {len(segments)} transcript segments", progress=2, started_at=now_iso(), error="")
            candidates: list[dict] = []
            async with self.jobs._heavy_operation:
                for index, chunk in enumerate(chunks, start=1):
                    if self.jobs.cancelled(job_id):
                        raise JobCancelled("Highlight detection cancelled by user.")
                    per_chunk = max(2, min(5, request.count - len(candidates) + 1))
                    prompt = PROMPT_TEMPLATE.format(
                        limit=per_chunk, min_duration=int(request.min_duration),
                        max_duration=int(request.max_duration), categories=", ".join(CATEGORIES),
                        chunk_start=chunk["start"], chunk_end=chunk["end"], lines=_render_chunk(chunk),
                    )
                    self.jobs.update(
                        job_id,
                        stage=f"Ollama {request.model}: chunk {index}/{len(chunks)}",
                        progress=round(3 + (index - 1) / max(1, len(chunks)) * 80, 2),
                    )
                    try:
                        response = await asyncio.to_thread(chat_json, request.model, prompt, system=HIGHLIGHT_SYSTEM)
                    except OllamaError as exc:
                        raise IngestionError(str(exc)) from exc
                    raw_items = response.get("candidates", []) if isinstance(response, dict) else response
                    if not isinstance(raw_items, list):
                        raw_items = []
                    for raw in raw_items:
                        if not isinstance(raw, dict):
                            continue
                        validated = _score_candidate(raw, segments, request, duration)
                        if validated:
                            candidates.append(validated)
                    self.jobs.update(job_id, progress=round(3 + index / max(1, len(chunks)) * 80, 2))
            if self.jobs.cancelled(job_id):
                raise JobCancelled("Highlight detection cancelled by user.")
            self.jobs.update(job_id, stage="ranking and removing duplicate moments", progress=88)
            deduped = _dedupe(candidates)
            best = _select_best(deduped, request.count, request.max_duration)
            if not best and deduped:
                best = deduped[: request.count]
            if not best:
                raise IngestionError(
                    "The local model did not return any valid clip moments. Try a longer transcript, "
                    "a lower quality threshold via fewer clips, or a different installed Ollama model."
                )
            self.jobs.update(job_id, stage="saving ranked candidates", progress=96)
            self._save_candidates(project_id, deduped, {item["title"] + str(item["start_time"]) for item in best}, request.model)
            elapsed = round(time.perf_counter() - started, 2)
            self.jobs.update(
                job_id, state="completed", stage=f"{len(best)} selected from {len(deduped)} candidates in {elapsed:.1f}s",
                progress=100, completed_at=now_iso(), cancel_requested=0,
            )
        except (IngestionError, JobCancelled) as exc:
            self.jobs.update(
                job_id, state="cancelled" if isinstance(exc, JobCancelled) else "failed",
                stage="cancelled" if isinstance(exc, JobCancelled) else "failed",
                error=str(exc), completed_at=now_iso(),
            )
        except Exception as exc:
            self.jobs.update(job_id, state="failed", stage="failed", error=f"Highlight detection failed: {exc}", completed_at=now_iso())

    def _save_candidates(self, project_id: str, candidates: list[dict], selected_keys: set[str], model: str) -> None:
        now = now_iso()
        with connect() as conn:
            # Keep approved candidates and any candidate already referenced by a clip.
            conn.execute(
                "DELETE FROM candidates WHERE project_id=? AND state != 'approved' "
                "AND id NOT IN (SELECT candidate_id FROM clips WHERE candidate_id IS NOT NULL)",
                (project_id,),
            )
            for candidate in candidates:
                key = candidate["title"] + str(candidate["start_time"])
                selected = 1 if key in selected_keys else 0
                conn.execute(
                    """INSERT INTO candidates(id,project_id,start_time,end_time,title,hook,reason,category,scores_json,
                       overall_score,excerpt,state,selected,source,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uuid.uuid4().hex, project_id, candidate["start_time"], candidate["end_time"],
                        candidate["title"], candidate["hook"], candidate["reason"], candidate["category"],
                        json.dumps(candidate["scores"]), candidate["overall_score"], candidate["excerpt"],
                        "candidate", selected, f"ollama:{model}", now, now,
                    ),
                )

    def update_candidate(self, candidate_id: str, payload: dict) -> dict:
        candidate = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not candidate:
            raise IngestionError("Candidate not found.")
        content = self._transcript_segments(candidate["project_id"])
        segments = content["segments"]
        duration = float(content.get("duration_seconds") or 0)
        updates: dict[str, Any] = {}
        if "title" in payload:
            updates["title"] = _clean_text(payload.get("title"), 120) or candidate["title"]
        if "hook" in payload:
            updates["hook"] = _clean_text(payload.get("hook"), 240) or candidate["hook"]
        if "reason" in payload:
            updates["reason"] = _clean_text(payload.get("reason"), 500) or candidate["reason"]
        if "category" in payload:
            updates["category"] = _category(payload.get("category"), candidate.get("excerpt", ""))
        if "start_time" in payload or "end_time" in payload:
            start = _clamp(_as_float(payload.get("start_time"), candidate["start_time"]), 0, duration)
            end = _clamp(_as_float(payload.get("end_time"), candidate["end_time"]), 0, duration)
            snapped = _snap_to_transcript(segments, start, end)
            if not snapped or snapped[1] - snapped[0] < 5:
                raise IngestionError("The edited timestamps do not overlap a usable transcript segment.")
            updates["start_time"], updates["end_time"] = round(snapped[0], 3), round(snapped[1], 3)
            updates["excerpt"] = _excerpt_for(segments, snapped[0], snapped[1])
        if not updates:
            return self.get_candidate(candidate_id)
        updates["updated_at"] = now_iso()
        assignments = ", ".join(f"{key}=?" for key in updates)
        with connect() as conn:
            conn.execute(f"UPDATE candidates SET {assignments} WHERE id=?", (*updates.values(), candidate_id))
        return self.get_candidate(candidate_id)

    def get_candidate(self, candidate_id: str) -> dict:
        item = row("SELECT * FROM candidates WHERE id=?", (candidate_id,))
        if not item:
            raise IngestionError("Candidate not found.")
        item["scores"] = json.loads(item.pop("scores_json", "{}") or "{}")
        item["selected"] = bool(item.get("selected"))
        return item

    def set_state(self, candidate_id: str, state: str) -> dict:
        if state not in {"candidate", "approved", "rejected"}:
            raise IngestionError("Candidate state must be candidate, approved or rejected.")
        candidate = self.get_candidate(candidate_id)
        with connect() as conn:
            conn.execute(
                "UPDATE candidates SET state=?,selected=?,updated_at=? WHERE id=?",
                (state, 0 if state == "rejected" else candidate["selected"], now_iso(), candidate_id),
            )
        if state == "approved":
            self.ensure_clip(candidate)
        return self.get_candidate(candidate_id)

    def ensure_clip(self, candidate: dict) -> dict:
        existing = row("SELECT * FROM clips WHERE candidate_id=?", (candidate["id"],))
        if existing:
            return existing
        now = now_iso()
        clip_id = uuid.uuid4().hex
        with connect() as conn:
            conn.execute(
                """INSERT INTO clips(id,project_id,candidate_id,title,start_time,end_time,state,preset,
                   caption_style,caption_options_json,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    clip_id, candidate["project_id"], candidate["id"], candidate["title"],
                    candidate["start_time"], candidate["end_time"], "draft",
                    settings.default_render_preset, settings.default_caption_style, "{}", now, now,
                ),
            )
        return row("SELECT * FROM clips WHERE id=?", (clip_id,)) or {}

    def create_clip(self, project_id: str, payload: dict) -> dict:
        candidate_id = payload.get("candidate_id")
        if candidate_id:
            candidate = self.get_candidate(str(candidate_id))
            if candidate["project_id"] != project_id:
                raise IngestionError("That candidate belongs to another project.")
            return self.ensure_clip(candidate)
        start = _as_float(payload.get("start_time"), -1)
        end = _as_float(payload.get("end_time"), -1)
        if start < 0 or end <= start:
            raise IngestionError("Provide a candidate_id or valid start_time/end_time.")
        content = self._transcript_segments(project_id)
        snapped = _snap_to_transcript(content["segments"], start, end)
        if not snapped:
            raise IngestionError("The requested timestamps do not overlap transcript content.")
        now = now_iso()
        clip_id = uuid.uuid4().hex
        with connect() as conn:
            conn.execute(
                """INSERT INTO clips(id,project_id,title,start_time,end_time,state,preset,caption_style,
                   caption_options_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    clip_id, project_id, _clean_text(payload.get("title"), 120) or "Untitled clip",
                    round(snapped[0], 3), round(snapped[1], 3), "draft",
                    settings.default_render_preset, settings.default_caption_style, "{}", now, now,
                ),
            )
        return row("SELECT * FROM clips WHERE id=?", (clip_id,)) or {}


def recover_interrupted_highlights() -> int:
    with connect() as conn:
        cursor = conn.execute(
            "UPDATE jobs SET state='failed',stage='interrupted by restart',error='The local server restarted during highlight detection. Retry this job.',completed_at=? "
            "WHERE kind='highlights' AND state IN ('queued','running')",
            (now_iso(),),
        )
        return cursor.rowcount
