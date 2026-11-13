"""Real local end-to-end workflow check.

Run the backend first, then:
    .venv\\Scripts\\python.exe tests/e2e_workflow.py

The script uploads a local authorized test podcast, transcribes it with the
already-installed local Whisper model, detects highlights with local Ollama,
approves candidates, renders vertical clips, validates the media and exports a
project ZIP. It never uses mock data.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8026"
MEDIA = Path(__file__).resolve().parent.parent / "temp" / "e2e_podcast.mp4"
MODEL = "small"
HIGHLIGHT_MODEL = "gemma3:4b"


def wait_health() -> dict:
    for _ in range(60):
        try:
            response = httpx.get(f"{BASE}/api/health", timeout=3)
            if response.status_code == 200:
                return response.json()
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise SystemExit("Backend did not start.")


def wait_job(client: httpx.Client, job_id: str, timeout: float = 1800) -> dict:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        job = client.get(f"{BASE}/api/jobs/{job_id}").json()
        marker = f"{job['state']} {job['stage']} {job['progress']}"
        if marker != last:
            print(f"  [{job['state']}] {job['stage']} ({job['progress']}%)", flush=True)
            last = marker
        if job["state"] in {"completed", "failed", "cancelled"}:
            return job
        time.sleep(2)
    raise SystemExit(f"Job {job_id} timed out.")


def probe(path: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration,size:stream=codec_type,codec_name,width,height",
         "-of", "json", path],
        capture_output=True, text=True,
    )
    return json.loads(result.stdout or "{}")


def main() -> None:
    if not MEDIA.is_file():
        raise SystemExit(f"Missing test media: {MEDIA}. Run scripts/generate_test_podcast.py first.")
    health = wait_health()
    print("health", json.dumps(health["ollama"]))
    client = httpx.Client(timeout=httpx.Timeout(30, read=1800, write=1800))

    project = client.post(f"{BASE}/api/projects", json={"name": "E2E Local Podcast", "source_url": ""}).json()
    print("project", project["id"])
    job = client.post(f"{BASE}/api/projects/{project['id']}/jobs", json={"kind": "ingestion"}).json()
    with MEDIA.open("rb") as media:
        response = client.post(
            f"{BASE}/api/projects/{project['id']}/source/upload",
            data={"job_id": job["id"]},
            files={"file": (MEDIA.name, media, "video/mp4")},
        )
    response.raise_for_status()
    job = wait_job(client, job["id"])
    assert job["state"] == "completed", job
    detail = client.get(f"{BASE}/api/projects/{project['id']}").json()
    print("source", detail["source_filename"], detail["metadata_json"][:200])

    print("transcribing")
    job = client.post(
        f"{BASE}/api/projects/{project['id']}/transcription",
        json={"model": MODEL, "language": "en", "word_timestamps": True, "device": "cpu"},
    ).json()
    job = wait_job(client, job["id"])
    assert job["state"] == "completed", job
    transcript = client.get(f"{BASE}/api/projects/{project['id']}/transcript").json()
    print("transcript", transcript["metadata"]["language"], transcript["metadata"]["word_count"],
          "words,", transcript["metadata"]["segment_count"], "segments, RTF", transcript["metadata"]["real_time_factor"])
    assert transcript["metadata"]["word_count"] > 500, "Transcript is too short for this 6-minute sample."
    assert transcript["content"]["language"] == "en", "Language detection did not return English."
    segments = transcript["content"]["segments"]
    assert all(0 <= item["start"] < item["end"] for item in segments), "Invalid timestamps."

    print("detecting highlights with", HIGHLIGHT_MODEL)
    job = client.post(
        f"{BASE}/api/projects/{project['id']}/highlights",
        json={"model": HIGHLIGHT_MODEL, "count": 5, "min_duration": 20, "max_duration": 60},
    ).json()
    job = wait_job(client, job["id"])
    assert job["state"] == "completed", job
    candidates = client.get(f"{BASE}/api/projects/{project['id']}/candidates").json()
    print("candidates", len(candidates))
    for item in candidates[:6]:
        print(f"  {item['overall_score']:>3} {item['start_time']:7.1f}-{item['end_time']:7.1f} {item['category']}: {item['title']}")
    assert candidates, "No highlight candidates returned."
    duration = transcript["content"]["duration_seconds"]
    for item in candidates:
        assert 0 <= item["start_time"] < item["end_time"] <= duration + 0.5, item
        assert item["excerpt"], "Candidate has no transcript excerpt."

    approved = []
    for item in candidates[:3]:
        result = client.post(f"{BASE}/api/candidates/{item['id']}/approve").json()
        approved.append(result)
    project_detail = client.get(f"{BASE}/api/projects/{project['id']}").json()
    print("clips created", len(project_detail["clips"]))
    assert len(project_detail["clips"]) >= min(3, len(candidates))

    rendered = []
    for clip in project_detail["clips"][:2]:
        print("rendering", clip["id"], clip["title"])
        job = client.post(
            f"{BASE}/api/clips/{clip['id']}/render",
            json={
                "preset": "podcast", "caption_style": "kinetic", "caption_options": {},
                "music_path": "", "music_volume": 0.18, "fps": 30, "ducking": True,
                "metadata_model": HIGHLIGHT_MODEL, "smart_reframe": True,
            },
        ).json()
        job = wait_job(client, job["id"], timeout=1200)
        assert job["state"] == "completed", job
        rendered.append(client.get(f"{BASE}/api/clips/{clip['id']}").json())

    for clip in rendered:
        assert Path(clip["output_path"]).is_file()
        info = probe(clip["output_path"])
        videos = [item for item in info["streams"] if item["codec_type"] == "video"]
        audios = [item for item in info["streams"] if item["codec_type"] == "audio"]
        assert videos and videos[0]["width"] == 1080 and videos[0]["height"] == 1920, info
        assert audios, info
        assert Path(clip["srt_path"]).is_file()
        assert Path(clip["thumbnail_path"]).is_file()
        metadata = client.get(f"{BASE}/api/clips/{clip['id']}/metadata").json()
        print("rendered", clip["id"], round(clip["duration"], 2), "s", clip["file_size_bytes"], "bytes", metadata.get("title"))
        assert metadata.get("description")
        assert metadata.get("hashtags")

    exported = client.post(f"{BASE}/api/clips/{rendered[0]['id']}/export").json()
    print("single export", exported["directory"], len(exported["files"]))
    project_export = client.post(f"{BASE}/api/projects/{project['id']}/export").json()
    print("project export", project_export)
    assert Path(project_export["path"]).is_file() and Path(project_export["path"]).stat().st_size > 1000
    exports = client.get(f"{BASE}/api/exports").json()
    print("export files", len(exports["files"]))
    print("E2E PASSED")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print("E2E FAILED", exc, file=sys.stderr)
        raise
