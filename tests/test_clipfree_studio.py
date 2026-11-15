from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.boundaries import snap_boundaries
from backend.captions import CaptionOptions, _fit_font_size, build_ass, clip_srt
from backend.config import settings
from backend.database import connect, get_setting, init_db, now_iso, row, set_setting
from backend.highlights import (
    HighlightRequest,
    _dedupe,
    _score_candidate,
    _select_best,
)
from backend.rendering import ClipService
from backend.ingestion import ffmpeg_progress_seconds
from backend.metadata import generate_clip_metadata
from backend.ollama import OllamaError
from backend.reframe import (
    _piecewise_expression,
    build_vertical_filter,
    detect_face_track,
    ensure_yunet_model,
    _load_yunet,
    yunet_model_path,
)
from backend.social import write_viral_captions


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


def test_yunet_detector_loads_and_detect_face_track_fails_safe() -> None:
    # The small YuNet model is fetched into the local models folder once.
    model = ensure_yunet_model()
    if model is None:
        pytest.skip("YuNet model download unavailable in this environment.")
    assert model == yunet_model_path() and model.is_file() and model.stat().st_size > 100_000
    try:
        import cv2
    except Exception:
        pytest.skip("OpenCV is not installed.")
    if not hasattr(cv2, "FaceDetectorYN_create"):
        pytest.skip("OpenCV without the face detection module.")
    detector = _load_yunet(cv2)
    assert detector is not None
    # A missing video never crashes - it simply yields no track.
    assert detect_face_track(Path("does-not-exist.mp4"), 0.0, 5.0) == []


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


def test_clip_cuts_snap_to_pauses_and_complete_thoughts() -> None:
    segments = [
        {
            "start": 0.0, "end": 2.6, "text": "One idea lands here.",
            "words": [
                {"start": 0.0, "end": 0.4, "word": "One"},
                {"start": 0.5, "end": 1.0, "word": "idea"},
                {"start": 1.1, "end": 1.7, "word": "lands"},
                {"start": 1.8, "end": 2.6, "word": "here."},
            ],
        },
        {
            "start": 3.4, "end": 6.6, "text": "This second thought keeps going",
            "words": [
                {"start": 3.4, "end": 3.7, "word": "This"},
                {"start": 3.8, "end": 4.4, "word": "second"},
                {"start": 4.5, "end": 5.1, "word": "thought"},
                {"start": 5.2, "end": 5.7, "word": "keeps"},
                {"start": 5.8, "end": 6.6, "word": "going"},
            ],
        },
    ]
    # An end landing inside a word moves to that word's clean end.
    assert snap_boundaries(segments, 3.4, 6.2) == (3.4, 6.6)
    # A mid-thought end completes the sentence instead (with room for a longer clip).
    assert snap_boundaries(segments, 0.0, 2.0, min_length=2.0) == (0.0, 2.6)
    # A start landing mid-sentence pulls back to the start of the thought.
    assert snap_boundaries(segments, 4.0, 6.6) == (3.4, 6.6)
    # No pause or sentence boundary within reach keeps the requested timing.
    assert snap_boundaries(segments, 4.9, 5.3, min_length=0.3) == (4.9, 5.3)
    # Never shrink a clip below the minimum length while snapping.
    assert snap_boundaries(segments, 0.0, 1.0, min_length=3.0) == (0.0, 1.0)


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


def test_viral_captions_are_grounded_and_fall_back_without_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> dict:
        raise OllamaError("Ollama offline")

    monkeypatch.setattr("backend.social.chat_json", unavailable)
    result = write_viral_captions(
        {"title": "Shark Tank Success Story", "duration": 15},
        "In 2020, two founders walked onto Shark Tank and rejected a $1 million offer.",
        "gemma3:4b",
    )
    assert result["generated_by"] == "local-fallback"
    variants = result["variants"]
    assert len(variants) >= 3
    styles = [item["style"] for item in variants]
    assert styles[0] == "curiosity" and "stat" in styles
    for variant in variants:
        assert variant["hook"] and variant["caption"] and variant["cta"]
        assert 3 <= len(variant["hashtags"]) <= 8
        assert all(tag.startswith("#") for tag in variant["hashtags"])
    assert "$1 million" in variants[0]["caption"]


def test_viral_captions_use_the_local_model_and_reject_malformed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    good = {
        "variants": [
            {
                "style": "curiosity", "hook": "Why they turned down $1M",
                "caption": "They said no, and it worked.", "hashtags": ["#SharkTank", "#Business", "#Gum"],
                "cta": "Save this clip.",
            },
            {
                "style": "contrarian", "hook": "The wrong advice is everywhere",
                "caption": "Here is what actually happened.", "hashtags": ["#Podcast", "#Money", "#Growth"],
                "cta": "Share it with a founder.",
            },
        ]
    }
    monkeypatch.setattr("backend.social.chat_json", lambda *a, **k: good)
    result = write_viral_captions({"title": "Clip", "duration": 12}, "A transcript excerpt.", "gemma3:4b")
    assert result["generated_by"] == "ollama"
    assert result["variants"][0]["hook"] == "Why they turned down $1M"
    assert result["variants"][0]["label"] == "Curiosity gap"

    monkeypatch.setattr("backend.social.chat_json", lambda *a, **k: {"variants": [{"hook": ""}]})
    broken = write_viral_captions({"title": "Clip", "duration": 12}, "A transcript excerpt.", "gemma3:4b")
    assert broken["generated_by"] == "local-fallback"


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


def test_pipeline_start_requires_source_and_rejects_duplicates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "pipeline-guard"))
    from backend.main import app

    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "Pipeline Guard"}).json()["id"]

        # Without a local source the pipeline refuses to start.
        refused = client.post(f"/api/projects/{project_id}/pipeline", json={})
        assert refused.status_code == 409
        assert "source" in refused.json()["detail"].lower()

        # A project with an active pipeline run cannot start a second one.
        active = client.post("/api/projects", json={"name": "Pipeline Busy"}).json()["id"]
        source = settings.projects_dir / active / "source" / "episode.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"0" * 1024)
        with connect() as conn:
            conn.execute(
                "UPDATE projects SET source_path=?,status='source_ready' WHERE id=?", (str(source), active)
            )
            conn.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
                ("active-pipeline", active, "pipeline", "running", "rendering", 50, now_iso()),
            )
        duplicate = client.post(f"/api/projects/{active}/pipeline", json={})
        assert duplicate.status_code == 409
        assert "already active" in duplicate.json()["detail"].lower()


def test_full_pipeline_skips_finished_stages_and_exports_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "pipeline-data"))
    from backend.main import app

    async def fake_render(self: ClipService, job_id: str, clip_id: str, request) -> None:
        clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
        output = settings.projects_dir / clip["project_id"] / "clips" / clip_id / f"{clip_id}.mp4"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake rendered mp4 bytes")
        with connect() as conn:
            conn.execute(
                "UPDATE clips SET state='rendered',output_path=?,duration=20.0,error='',updated_at=? WHERE id=?",
                (str(output), now_iso(), clip_id),
            )
            conn.execute(
                "UPDATE jobs SET state='completed',stage='done',progress=100,completed_at=?,cancel_requested=0 WHERE id=?",
                (now_iso(), job_id),
            )

    monkeypatch.setattr(ClipService, "run", fake_render)

    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "Pipeline E2E"}).json()["id"]
        project_dir = settings.projects_dir / project_id
        source = project_dir / "source" / "episode.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"0" * 2048)

        transcript_dir = project_dir / "transcript"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / "transcript.json"
        transcript_path.write_text(json.dumps(TRANSCRIPT), encoding="utf-8")

        now = now_iso()
        with connect() as conn:
            conn.execute(
                "INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
                ("seed-job", project_id, "transcription", "completed", "seeded", 100, now),
            )
            conn.execute(
                "UPDATE projects SET source_path=?,source_title='episode',status='source_ready' WHERE id=?",
                (str(source), project_id),
            )
            # Pre-seed transcript and candidates so those stages are skipped.
            conn.execute(
                """INSERT INTO transcripts(project_id,job_id,model_name,language,device,compute_type,json_path,srt_path,
                   text_path,duration_seconds,segment_count,word_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, "seed-job", "small", "en", "cpu", "int8", str(transcript_path),
                 str(transcript_dir / "transcript.srt"), str(transcript_dir / "transcript.txt"),
                 60.0, 3, 24, now, now),
            )
            for index, (start, end, title, score) in enumerate([
                (10.0, 40.0, "First moment", 90), (22.0, 58.0, "Second moment", 80),
            ]):
                conn.execute(
                    """INSERT INTO candidates(id,project_id,start_time,end_time,title,hook,reason,category,scores_json,
                       overall_score,excerpt,state,selected,source,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"cand-{index}", project_id, start, end, title, "hook", "reason", "Motivational",
                     '{"hook": 9}', score, "excerpt", "candidate", 1, "ollama:test", now, now),
                )

        started = client.post(f"/api/projects/{project_id}/pipeline", json={"count": 2})
        assert started.status_code == 202, started.text
        job = started.json()
        assert job["kind"] == "pipeline"
        job_id = job["id"]

        final = None
        for _ in range(60):
            final = client.get(f"/api/jobs/{job_id}").json()
            if final["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.25)
        assert final is not None and final["state"] == "completed", final
        result = json.loads(final.get("payload_json") or "{}").get("result", {})
        assert result.get("rendered") == 2

        clips = client.get(f"/api/clips?project_id={project_id}").json()
        assert len(clips) == 2 and all(clip["state"] == "rendered" for clip in clips)

        zips = list((project_dir / "exports").glob("*.zip"))
        assert len(zips) == 1
        with zipfile.ZipFile(zips[0]) as archive:
            names = archive.namelist()
        assert any(name.endswith(".mp4") for name in names)

        # The ZIP is downloadable through the project export endpoint.
        download = client.get(
            f"/api/projects/{project_id}/export/download?name={zips[0].name}"
        )
        assert download.status_code == 200
