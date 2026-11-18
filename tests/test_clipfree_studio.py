from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.silence import concat_filters, plan_trim
from backend.boundaries import snap_boundaries
from backend.captions import CaptionOptions, _fit_font_size, _position_y, build_ass, clip_srt
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


def test_render_writes_metadata_before_thumbnail_and_burns_hook_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "thumb-meta"))
    import backend.rendering as rendering_module
    from backend.rendering import ClipService

    order: list[str] = []
    seen: dict[str, str] = {}

    def fake_metadata(payload, excerpt, model):
        order.append("metadata")
        return {"title": "T", "description": "D", "hashtags": ["#Shorts"],
                "category": "Motivational", "thumbnail_text": "BIG HOOK TEXT"}

    def fake_thumbnail(video_path, output_path, text, timestamp=1.0):
        order.append("thumbnail")
        seen["text"] = text
        output_path.write_bytes(b"jpg")
        return output_path

    async def fake_render_video(self, job_id, source, temporary, *args, **kwargs):
        order.append("video")
        Path(temporary).write_bytes(b"fake mp4")

    def fake_probe(path, *args, **kwargs):
        info = {
            "duration_seconds": 60.0, "file_size_bytes": 1024,
            "video": {"width": 1920, "height": 1080}, "audio": {"available": True},
        }
        if "partial" in Path(str(path)).name:
            info["duration_seconds"] = 25.0
            info["video"] = {"width": 1080, "height": 1920}
        return info

    monkeypatch.setattr(rendering_module, "generate_clip_metadata", fake_metadata)
    monkeypatch.setattr(rendering_module, "create_thumbnail", fake_thumbnail)
    monkeypatch.setattr(rendering_module, "probe_media", fake_probe)
    monkeypatch.setattr(ClipService, "_render_video", fake_render_video)

    from backend.main import app
    with TestClient(app) as client:
        project_id = client.post("/api/projects", json={"name": "Thumb Meta"}).json()["id"]
        project_dir = settings.projects_dir / project_id
        source = project_dir / "source" / "episode.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"0" * 2048)
        transcript_dir = project_dir / "transcript"
        transcript_dir.mkdir(parents=True, exist_ok=True)
        transcript_path = transcript_dir / "transcript.json"
        transcript_path.write_text(json.dumps(TRANSCRIPT), encoding="utf-8")
        now = now_iso()
        clip_id = "clip-thumb-test"
        with connect() as conn:
            conn.execute("INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
                         ("seed-job", project_id, "transcription", "completed", "seeded", 100, now))
            conn.execute("UPDATE projects SET source_path=?,status='source_ready' WHERE id=?", (str(source), project_id))
            conn.execute(
                """INSERT INTO transcripts(project_id,job_id,model_name,language,device,compute_type,json_path,srt_path,
                   text_path,duration_seconds,segment_count,word_count,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (project_id, "seed-job", "small", "en", "cpu", "int8", str(transcript_path),
                 str(transcript_dir / "t.srt"), str(transcript_dir / "t.txt"), 60.0, 3, 24, now, now),
            )
            conn.execute(
                """INSERT INTO clips(id,project_id,title,start_time,end_time,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (clip_id, project_id, "Hook test", 10.0, 35.0, "draft", now, now),
            )

        started = client.post(f"/api/clips/{clip_id}/render", json={"smart_reframe": False})
        assert started.status_code == 202, started.text
        job_id = started.json()["id"]
        final = None
        for _ in range(80):
            final = client.get(f"/api/jobs/{job_id}").json()
            if final["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.1)
        assert final is not None and final["state"] == "completed", (final or {}).get("error") or final

    # Metadata is generated first so the thumbnail can burn its hook text.
    assert order == ["video", "metadata", "thumbnail"]
    assert seen["text"] == "BIG HOOK TEXT"
    clip = row("SELECT * FROM clips WHERE id=?", (clip_id,))
    assert clip["state"] == "rendered"
    assert json.loads(clip["metadata_json"])["thumbnail_text"] == "BIG HOOK TEXT"
    assert clip["thumbnail_path"] and Path(clip["thumbnail_path"]).is_file()


def test_metadata_includes_three_distinct_title_variants() -> None:
    clip = {"title": "Working title", "category": "Motivational"}
    excerpt = "Two founders walked onto Shark Tank and changed their company forever. Investors were shocked."
    raw = {
        "title": "Working title",
        "title_variants": [
            "What Shark Tank taught two founders",
            "The number that shocked the investors",
            "Working title",                      # duplicate of the main title
            "  ",                                  # blank is dropped
        ],
        "description": "Accurate description.", "hashtags": ["#Shorts", "#Podcast"],
        "category": "Motivational", "thumbnail_text": "SHARK TANK SHOCK",
    }
    from backend.metadata import _validate
    validated = _validate(raw, clip, excerpt)
    assert validated is not None
    variants = validated["title_variants"]
    assert variants[0] == "Working title"
    assert len(variants) == 4 or len(variants) == 3   # title + up to 3 distinct angles
    assert len(set(variants)) == len(variants)
    assert "What Shark Tank taught two founders" in variants

    # The local fallback also exposes variants so the studio always has choices.
    from backend.metadata import _fallback
    fallback = _fallback(clip, excerpt)
    assert fallback["title_variants"][0] == clip["title"]
    assert len(fallback["title_variants"]) >= 2


def test_silence_plan_cuts_dead_air_and_retimes_captions() -> None:
    # 30 s clip with a 5 s dead-air hole between two sentences.
    content = {
        "language": "en", "duration_seconds": 60.0,
        "segments": [
            {"start": 0.0, "end": 12.0, "text": "First part ends here.",
             "words": [
                 {"start": 0.0, "end": 3.0, "word": "First"},
                 {"start": 3.2, "end": 6.0, "word": "part"},
                 {"start": 6.2, "end": 9.0, "word": "ends"},
                 {"start": 9.2, "end": 12.0, "word": "here."},
             ]},
            {"start": 17.0, "end": 30.0, "text": "Second part continues.",
             "words": [
                 {"start": 17.0, "end": 20.0, "word": "Second"},
                 {"start": 20.2, "end": 23.0, "word": "part"},
                 {"start": 23.2, "end": 27.0, "word": "continues."},
             ]},
        ],
    }
    plan = plan_trim(content, 0.0, 30.0)
    assert plan is not None
    pieces = plan["pieces"]
    # Two pieces: everything up to just after "here." and from just before "Second".
    assert len(pieces) == 2
    assert abs(pieces[0][0] - 0.0) < 0.01
    assert abs(pieces[0][1] - 12.35) < 0.01   # keep 0.35 s of pause after the word
    assert abs(pieces[1][0] - 16.95) < 0.01   # start just before the next word
    assert abs(pieces[1][1] - 30.0) < 0.01
    # The trimmed clip is ~5.4 s shorter and captions are re-timed onto it.
    assert 24.0 < plan["duration"] < 25.5
    remapped = plan["content"]["segments"]
    assert abs(remapped[1]["start"] - 12.4) < 0.05   # 12.35 offset + 0.05 guard
    assert remapped[1]["words"][0]["word"] == "Second"

    # FFmpeg chains concatenate both streams.
    video, vlabel = concat_filters(pieces, "0:v", "video")
    audio, alabel = concat_filters(pieces, "0:a", "audio")
    assert "[vc]" == vlabel and "[ac]" == alabel
    assert "concat=n=2:v=1:a=0[vc]" in video
    assert "concat=n=2:v=0:a=1[ac]" in audio
    assert "trim=start=0.000:end=12.350" in video
    assert "atrim=start=0.000:end=12.350" in audio

    # No long gaps -> nothing to cut.
    tight = {"segments": [{"start": 0.0, "end": 10.0, "text": "a b", "words": [
        {"start": 0.0, "end": 4.0, "word": "a"}, {"start": 4.2, "end": 8.0, "word": "b"}]}]}
    assert plan_trim(tight, 0.0, 10.0) is None


def test_platform_presets_lift_captions_for_reels_safe_area() -> None:
    options = CaptionOptions(margin_v=260)
    youtube = build_ass(TRANSCRIPT, 10.0, 58.0, "kinetic", CaptionOptions(platform="youtube", margin_v=260))
    reels = build_ass(TRANSCRIPT, 10.0, 58.0, "kinetic", CaptionOptions(platform="reels", margin_v=260))
    youtube_y = _position_y("bottom", 260, "youtube")
    reels_y = _position_y("bottom", 260, "reels")
    assert f"\\pos(540,{youtube_y})" in youtube
    assert f"\\pos(540,{reels_y})" in reels
    # Reels' UI covers more of the lower frame, so captions sit higher.
    assert reels_y < youtube_y - 100
    assert options is not None


def test_hindi_captions_use_nirmala_ui_and_skip_uppercasing() -> None:
    hindi = {
        "language": "hi",
        "duration_seconds": 20.0,
        "segments": [{
            "start": 0.0, "end": 20.0, "text": "आपकी आदतें आपका भविष्य तय करती हैं।",
            "words": [
                {"start": 0.0, "end": 1.4, "word": "आपकी"},
                {"start": 1.5, "end": 2.8, "word": "आदतें"},
                {"start": 2.9, "end": 4.2, "word": "आपका"},
                {"start": 4.3, "end": 5.6, "word": "भविष्य"},
                {"start": 5.7, "end": 7.0, "word": "तय"},
                {"start": 7.1, "end": 9.0, "word": "करती"},
                {"start": 9.1, "end": 10.4, "word": "हैं।"},
            ],
        }],
    }
    ass = build_ass(hindi, 0.0, 20.0, "kinetic", CaptionOptions())
    assert "Nirmala UI" in ass
    assert "आपकी" in ass  # Devanagari text is preserved verbatim
    # English captions keep the style's own font.
    english = build_ass(TRANSCRIPT, 10.0, 58.0, "kinetic", CaptionOptions())
    assert "Nirmala UI" not in english and ",Arial," in english


def test_yunet_detector_loads_and_detect_face_track_fails_safe() -> None:    # The small YuNet model is fetched into the local models folder once.
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


def test_publish_request_validates_payload() -> None:
    from backend.publishing import PublishRequest
    from backend.ingestion import IngestionError

    request = PublishRequest.from_payload({
        "title": "My Short", "description": "Desc", "tags": [" #shorts ", "", "podcast"],
        "privacy": "unlisted", "category": "gaming",
    })
    assert request.title == "My Short"
    assert request.tags == ["#shorts", "podcast"]
    assert request.privacy == "unlisted"

    for payload in ({"privacy": "sneaky"}, {"category": "unknown"},
                    {"publish_at": "not-a-date"}, {"publish_at": "2020-01-01T00:00:00Z"}):
        with pytest.raises(IngestionError):
            PublishRequest.from_payload(payload)


def test_scheduled_publish_body_forces_private_until_publish_at() -> None:
    from datetime import datetime, timedelta, timezone

    from backend.publishing import _video_body

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(timespec="seconds")
    scheduled = _video_body({
        "title": "T", "description": "D", "tags": ["a"], "privacy": "public",
        "category_id": "27", "publish_at": future,
    })
    assert scheduled["status"]["privacyStatus"] == "private"
    assert scheduled["status"]["publishAt"].endswith("Z")
    assert scheduled["snippet"]["categoryId"] == "27"

    # Without publish_at the requested privacy applies and no schedule is set.
    immediate = _video_body({"title": "T", "privacy": "unlisted", "category_id": "24"})
    assert immediate["status"]["privacyStatus"] == "unlisted"
    assert "publishAt" not in immediate["status"]


def test_chat_json_retries_malformed_output(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend import ollama

    calls = {"n": 0}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            content = '{"broken": ' if calls["n"] == 1 else '{"ok": true}'
            return {"message": {"content": content}}

    class FakeClient:
        def __init__(self, *args, **kwargs) -> None:
            return None

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *args) -> bool:
            return False

        def post(self, url, json=None):
            calls["n"] += 1
            return FakeResponse()

    monkeypatch.setattr(ollama.httpx, "Client", FakeClient)
    assert ollama.chat_json("gemma3:4b", "prompt") == {"ok": True}
    assert calls["n"] == 2  # malformed first answer triggers the stricter retry


def test_publish_queue_requires_client_token_confirm_and_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "pub-data"))
    from backend.main import app

    with TestClient(app) as client:
        status = client.get("/api/publishing/status").json()
        assert status["client_configured"] is False
        assert status["authorized"] is False

        # OAuth cannot start without the user's own client_secret.json.
        auth = client.post("/api/publishing/auth/start")
        assert auth.status_code == 400
        assert "client_secret.json" in auth.json()["detail"]

        project_id = client.post("/api/projects", json={"name": "Publish Guard"}).json()["id"]
        clip_id = "seed-pub-clip"
        now = "2026-01-01T00:00:00+00:00"
        with connect() as conn:
            conn.execute(
                """INSERT INTO clips(id,project_id,title,start_time,end_time,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (clip_id, project_id, "Publish guard clip", 10.0, 40.0, "rendered", now, now),
            )

        # Explicit confirmation is mandatory — never publish silently.
        unconfirmed = client.post(f"/api/clips/{clip_id}/publish", json={"title": "X"})
        assert unconfirmed.status_code == 409
        assert "confirm=true" in unconfirmed.json()["detail"]

        confirmed = client.post(f"/api/clips/{clip_id}/publish?confirm=true", json={"title": "X"})
        assert confirmed.status_code == 409
        assert "Not authorized" in confirmed.json()["detail"]

        # Without a rendered file there is nothing to upload either.
        token = tmp_path / "pub-data" / "publishing" / "token.json"
        token.write_text(json.dumps({"token": "fake", "scopes": ["https://www.googleapis.com/auth/youtube.upload"]}), encoding="utf-8")
        monkeypatch.setattr("backend.publishing.PublishingService._load_credentials", lambda self: object())
        no_render = client.post(f"/api/clips/{clip_id}/publish?confirm=true", json={"title": "X"})
        assert no_render.status_code == 409
        assert "Render" in no_render.json()["detail"] or "rendered video" in no_render.json()["detail"]


def test_recover_interrupted_publishes_fails_stranded_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "rec-data"))
    from backend.publishing import recover_interrupted_publishes

    init_db()
    project_id = "proj-rec"
    now = "2026-01-01T00:00:00+00:00"
    with connect() as conn:
        conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     (project_id, "Recovery", now, now))
        conn.execute("INSERT INTO clips(id,project_id,title,start_time,end_time,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("clip-rec", project_id, "Rec", 0.0, 10.0, "publishing", now, now))
        conn.execute("INSERT INTO jobs(id,project_id,kind,state,stage,progress,payload_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     ("job-rec", project_id, "publish", "running", "uploading to YouTube", 30,
                      json.dumps({"clip_id": "clip-rec"}), now))

    recover_interrupted_publishes()
    job = row("SELECT * FROM jobs WHERE id='job-rec'")
    assert job["state"] == "failed"
    assert "restarted" in job["error"].lower()
    clip = row("SELECT * FROM clips WHERE id='clip-rec'")
    assert clip["state"] == "failed"


def test_transcript_segment_edits_retime_words_and_rewrite_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLIPFREE_DATA_DIR", str(tmp_path / "tx-data"))
    from backend.transcription import TranscriptionService
    from backend.jobs import IngestionJobManager

    init_db()
    project_id = "proj-tx"
    now = "2026-01-01T00:00:00+00:00"
    transcript_dir = tmp_path / "tx-data" / "projects" / project_id / "transcript"
    transcript_dir.mkdir(parents=True)
    content = {
        "language": "en", "duration_seconds": 30.0,
        "segments": [
            {"start": 0.0, "end": 10.0, "text": "Untouched segment.",
             "words": [{"start": 0.0, "end": 10.0, "word": "Untouched segment.", "probability": 0.9}]},
            {"start": 12.0, "end": 20.0, "text": "Old snsegment with a typo.",
             "words": [
                 {"start": 12.0, "end": 14.0, "word": "Old", "probability": 0.8},
                 {"start": 14.0, "end": 16.0, "word": "snsegment", "probability": 0.7},
                 {"start": 16.0, "end": 20.0, "word": "with a typo.", "probability": 0.9},
             ]},
        ],
    }
    json_path = transcript_dir / "transcript.json"
    srt_path = transcript_dir / "transcript.srt"
    text_path = transcript_dir / "transcript.txt"
    json_path.write_text(json.dumps(content), encoding="utf-8")
    srt_path.write_text("1\n00:00:12,000 --> 00:00:20,000\nOld snsegment with a typo.\n", encoding="utf-8")
    text_path.write_text("Old snsegment with a typo.\n", encoding="utf-8")
    with connect() as conn:
        conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES(?,?,?,?)",
                     (project_id, "Tx Edit", now, now))
        conn.execute("INSERT INTO jobs(id,project_id,kind,state,stage,progress,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("tx-job", project_id, "transcription", "completed", "seeded", 100, now))
        conn.execute(
            """INSERT INTO transcripts(project_id,job_id,model_name,language,device,compute_type,json_path,srt_path,
               text_path,duration_seconds,segment_count,word_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (project_id, "tx-job", "small", "en", "cpu", "int8", str(json_path), str(srt_path),
             str(text_path), 30.0, 2, 4, now, now),
        )

    service = TranscriptionService(IngestionJobManager())
    result = service.apply_segment_edits(project_id, [{"index": 1, "text": "Fixed segment without a typo."}])
    assert result["edited"] == 1 and result["segments"] == 2

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["segments"][0]["text"] == "Untouched segment."
    edited = saved["segments"][1]
    assert edited["text"] == "Fixed segment without a typo."
    words = edited["words"]
    assert len(words) == len("Fixed segment without a typo.".split())
    assert words[0]["start"] == pytest.approx(12.0)
    assert words[-1]["end"] == pytest.approx(20.0)
    for first, second in zip(words, words[1:]):
        assert second["start"] >= first["start"]
    assert "Fixed segment without a typo." in srt_path.read_text(encoding="utf-8")
    assert "Fixed segment without a typo." in text_path.read_text(encoding="utf-8")

    # Out-of-range indexes are skipped, and a no-op edit batch is rejected.
    with pytest.raises(Exception):
        service.apply_segment_edits(project_id, [{"index": 99, "text": "Nothing."}])
