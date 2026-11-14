from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.captions import CaptionOptions, _fit_font_size, build_ass, clip_srt
from backend.database import connect, get_setting, init_db, now_iso, set_setting
from backend.highlights import (
    HighlightRequest,
    _dedupe,
    _score_candidate,
    _select_best,
)
from backend.ingestion import ffmpeg_progress_seconds
from backend.metadata import generate_clip_metadata
from backend.ollama import OllamaError
from backend.reframe import _piecewise_expression, build_vertical_filter, detect_face_track


TRANSCRIPT = {
    "language": "en",
    "duration_seconds": 60.0,
    "segments": [
        {
            "start": 10.0, "end": 22.0,
            "text": "Here is a surprising fact about habits.",
            "words": [
                {"start": 10.0, "end": 10.4, "word": "Here"},
                {"start": 10.4, "end": 10.8, "word": "is"},
                {"start": 10.8, "end": 11.2, "word": "a"},
                {"start": 11.2, "end": 11.8, "word": "surprising"},
                {"start": 11.8, "end": 12.2, "word": "fact"},
                {"start": 12.2, "end": 22.0, "word": "about habits."},
            ],
        },
        {"start": 22.0, "end": 40.0, "text": "Your systems decide most of your daily behaviour."},
        {"start": 40.0, "end": 58.0, "text": "Inspect those systems instead of blaming your personality."},
    ],
}


def test_captions_render_word_synchronised_ass_and_clip_relative_srt() -> None:
    ass = build_ass(TRANSCRIPT, 10.0, 58.0, "kinetic", CaptionOptions())
    assert "PlayResX: 1080" in ass
    assert "PlayResY: 1920" in ass
    assert "Dialogue: 0,0:00:00.00" in ass
    assert "\\pos(540," in ass
    assert "\\c&H" in ass  # current-word highlight colour
    # Text colours must be opaque; FF alpha would render an invisible fill.
    assert "\\c&H00" in ass
    assert "\\c&HFF" not in ass
    assert "Surprising".upper() in ass or "SURPRISING" in ass
    # ASS times are clip-relative, not source-relative.
    assert "0:00:10" not in ass

    srt = clip_srt(TRANSCRIPT, 10.0, 58.0)
    assert "00:00:00,000 --> 00:00:12,000" in srt
    assert "Your systems decide" in srt
    assert srt.rstrip().endswith("Inspect those systems instead of blaming your personality.")


def test_caption_cues_never_overlap_so_glyphs_do_not_stack() -> None:
    overlapping = {
        "language": "en",
        "duration_seconds": 20.0,
        "segments": [{
            "start": 0.0, "end": 20.0, "text": "one two three four five six",
            "words": [
                {"start": 0.0, "end": 1.6, "word": "one"},
                {"start": 1.4, "end": 2.2, "word": "two"},
                {"start": 2.0, "end": 3.1, "word": "three"},
                {"start": 3.0, "end": 4.0, "word": "four"},
                {"start": 3.9, "end": 5.0, "word": "five"},
                {"start": 4.8, "end": 6.0, "word": "six"},
            ],
        }],
    }
    ass = build_ass(overlapping, 0.0, 20.0, "kinetic", CaptionOptions())

    def to_seconds(stamp: str) -> float:
        hours, minutes, seconds = stamp.split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)

    spans = []
    for line in ass.splitlines():
        if not line.startswith("Dialogue:"):
            continue
        head = line[len("Dialogue:"):].split(",,", 1)[0].split(",")
        spans.append((to_seconds(head[1]), to_seconds(head[2])))
    assert spans
    for index in range(1, len(spans)):
        assert spans[index][0] >= spans[index - 1][1] - 1e-6


def test_long_caption_lines_shrink_to_stay_inside_the_frame() -> None:
    assert _fit_font_size("OK", 76) == 76
    wide = _fit_font_size("VERY CHEASKED INSTRUCTOR", 76)
    assert wide < 76
    # Even extreme words never collapse below the readability floor.
    assert _fit_font_size("X" * 80, 76) >= int(76 * 0.62)
    ass = build_ass(TRANSCRIPT, 10.0, 58.0, "kinetic", CaptionOptions())
    assert "\\fs76" in ass or "\\fs7" in ass  # per-event size tag present


def test_ffmpeg_progress_skips_unavailable_timestamps() -> None:
    # FFmpeg writes N/A before the first frame; a render must not abort on it.
    assert ffmpeg_progress_seconds("out_time_ms", "N/A") is None
    assert ffmpeg_progress_seconds("out_time_us", "") is None
    assert ffmpeg_progress_seconds("frame", "12") is None
    assert ffmpeg_progress_seconds("out_time_ms", "2500000") == 2.5
    assert ffmpeg_progress_seconds("out_time_us", "900000") == 0.9


def test_reframe_uses_safe_centre_crop_without_face_track() -> None:
    framing = build_vertical_filter(1280, 720, 1080, 1920)
    assert framing["face_detected"] is False
    assert framing["filter"].startswith("scale=")
    assert "crop=1080:1920" in framing["filter"]
    assert framing["scaled_width"] >= 1080 and framing["scaled_height"] >= 1920


def test_reframe_builds_bounded_piecewise_pan_for_detected_faces() -> None:
    track = [
        {"time": 0.0, "x": 0.20, "y": 0.40},
        {"time": 5.0, "x": 0.50, "y": 0.45},
        {"time": 10.0, "x": 0.80, "y": 0.50},
    ]
    framing = build_vertical_filter(1280, 720, 1080, 1920, track=track)
    assert framing["face_detected"] is True
    assert "if(lt(t," in framing["filter"]
    assert framing["face_side"] == "bottom"

    expression = _piecewise_expression([(0.0, 100.0), (2.0, 300.0)])
    assert expression.startswith("if(lt(t,2.000),")
    assert "t-0.000" in expression


def test_face_detection_degrades_gracefully_when_opencv_detector_is_missing() -> None:
    # OpenCV 5 removed CascadeClassifier; the render pipeline must still work.
    assert detect_face_track(Path("does-not-exist.mp4"), 0.0, 5.0) == []


def test_highlight_scoring_snaps_to_real_segments_and_ranks_scores() -> None:
    request = HighlightRequest(model="gemma3:4b", count=5, min_duration=25, max_duration=60)
    candidate = _score_candidate(
        {
            "start_time": 11.5, "end_time": 39.0,
            "title": "Habits run your day",
            "hook": "Your systems decide most of your daily behaviour.",
            "reason": "A complete standalone insight.",
            "category": "surprising",
            "scores": {"hook": 92, "emotion": 60, "clarity": 88, "context": 80, "value": 90},
            "overall_score": 85,
        },
        TRANSCRIPT["segments"], request, 60.0,
    )
    assert candidate is not None
    assert candidate["start_time"] == 10.0
    assert candidate["end_time"] == 40.0
    assert candidate["category"] == "Surprising"
    assert candidate["title"] == "Habits run your day"
    assert 80 <= candidate["overall_score"] <= 95
    assert set(candidate["scores"]) == {"hook", "emotion", "clarity", "context", "value"}


def test_highlight_scoring_rejects_invalid_or_too_short_moments() -> None:
    request = HighlightRequest(model="gemma3:4b", count=5, min_duration=25, max_duration=60)
    assert _score_candidate({"start_time": 5, "end_time": 5}, TRANSCRIPT["segments"], request, 60.0) is None
    assert _score_candidate({"start_time": 10, "end_time": 13}, TRANSCRIPT["segments"], request, 60.0) is None
    assert _score_candidate({"start_time": 80, "end_time": 95}, TRANSCRIPT["segments"], request, 60.0) is None


def test_dedupe_and_selection_keep_the_strongest_non_overlapping_moments() -> None:
    strong = {
        "start_time": 10.0, "end_time": 40.0, "overall_score": 90,
        "title": "Habits run your day", "excerpt": TRANSCRIPT["segments"][0]["text"],
    }
    duplicate = {
        "start_time": 11.0, "end_time": 39.0, "overall_score": 84,
        "title": "Habits run your day", "excerpt": TRANSCRIPT["segments"][0]["text"],
    }
    separate = {
        "start_time": 40.0, "end_time": 58.0, "overall_score": 76,
        "title": "Inspect your systems", "excerpt": TRANSCRIPT["segments"][2]["text"],
    }
    deduped = _dedupe([duplicate, separate, strong])
    assert len(deduped) == 2
    assert deduped[0]["overall_score"] == 90
    selected = _select_best(deduped, 2, 60)
    assert [item["title"] for item in selected] == ["Habits run your day", "Inspect your systems"]


def test_metadata_falls_back_to_grounded_transcript_when_ollama_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> dict:
        raise OllamaError("Ollama offline")

    monkeypatch.setattr("backend.metadata.chat_json", unavailable)
    metadata = generate_clip_metadata(
        {"title": "Habits run your day", "category": "Insight"},
        "Your systems decide most of your daily behaviour and can be inspected.",
        "gemma3:4b",
    )
    assert metadata["generated_by"] == "local-fallback"
    assert metadata["title"] == "Habits run your day"
    assert metadata["description"].startswith("Your systems decide")
    assert metadata["hashtags"] == ["#Shorts", "#Podcast", "#Insight"]
    assert metadata["thumbnail_text"] == "HABITS RUN YOUR DAY"


def test_settings_round_trip_and_database_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "data"))
    init_db()
    assert get_setting("caption_style", "kinetic") == "kinetic"
    set_setting("caption_style", "cinematic")
    set_setting("clip_count", "7")
    assert get_setting("caption_style") == "cinematic"
    assert get_setting("clip_count") == "7"


def test_http_api_creates_project_and_exposes_studio_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "api-data"))
    from backend.main import app

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        created = client.post("/api/projects", json={"name": "API Test Project", "source_url": ""})
        assert created.status_code == 201
        project_id = created.json()["id"]

        detail = client.get(f"/api/projects/{project_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["name"] == "API Test Project"
        assert payload["candidates"] == [] and payload["clips"] == []
        assert payload["settings"]["caption_style"]
        assert payload["settings"]["highlight_model"]

        settings = client.get("/api/settings")
        assert settings.status_code == 200
        body = settings.json()
        assert body["presets"] and body["caption_styles"]
        assert body["values"]["render_concurrency"] >= 1

        updated = client.put("/api/settings", json={"caption_style": "podcast", "clip_count": 4})
        assert updated.status_code == 200
        assert updated.json()["values"]["caption_style"] == "podcast"
        assert updated.json()["values"]["clip_count"] == 4

        missing = client.get("/api/projects/not-a-real-project")
        assert missing.status_code == 404

        deleted = client.delete(f"/api/projects/{project_id}?confirm=true")
        assert deleted.status_code == 204
