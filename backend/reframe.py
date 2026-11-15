"""Practical face-aware vertical reframing with a safe centre-crop fallback.

Faces are found with the YuNet detector (a 232 KB ONNX model kept in the local
models folder), which is far more reliable than Haar cascades - especially for
profile shots and podcast two-camera setups. When YuNet or OpenCV's face
module is unavailable the classic Haar detector is used, and without any
detector a deterministic centre crop keeps rendering working.
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from statistics import median

from backend.config import settings

YUNET_FILENAME = "face_detection_yunet_2023mar.onnx"
YUNET_URL = (
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/"
    + YUNET_FILENAME
)


def yunet_model_path() -> Path:
    return settings.data_dir / "models" / "yunet" / YUNET_FILENAME


def ensure_yunet_model() -> Path | None:
    """Return the local YuNet model, downloading it once (~232 KB) if needed."""
    path = yunet_model_path()
    if path.is_file() and path.stat().st_size > 100_000:
        return path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(".partial")
        with urllib.request.urlopen(YUNET_URL, timeout=30) as response:
            partial.write_bytes(response.read())
        if partial.stat().st_size < 100_000:
            partial.unlink(missing_ok=True)
            return None
        partial.replace(path)
        return path
    except Exception:
        return None


def _load_yunet(cv2) -> object | None:
    creator = getattr(cv2, "FaceDetectorYN_create", None)
    if creator is None:
        return None
    model = ensure_yunet_model()
    if model is None:
        return None
    try:
        return creator(str(model), "", (320, 320), score_threshold=0.6, nms_threshold=0.3)
    except Exception:
        return None


def _load_haar(cv2):
    classifier_cls = getattr(cv2, "CascadeClassifier", None)
    haarcascades = getattr(getattr(cv2, "data", None), "haarcascades", "")
    cascade_file = Path(haarcascades) / "haarcascade_frontalface_default.xml" if haarcascades else None
    if classifier_cls is None or cascade_file is None or not cascade_file.is_file():
        return None
    cascade = classifier_cls(str(cascade_file))
    if cascade.empty():
        return None
    return cascade


def _detect_faces(frame, yunet, haar, width: int, height: int) -> list[tuple[int, int, int, int]]:
    """Faces as (x, y, w, h); YuNet first, Haar as the classic fallback."""
    if yunet is not None:
        try:
            frame_height, frame_width = frame.shape[:2]
            yunet.setInputSize((frame_width, frame_height))
            _, faces = yunet.detect(frame)
            if faces is not None and len(faces):
                return [(int(f[0]), int(f[1]), int(f[2]), int(f[3])) for f in faces if f[2] > 0]
        except Exception:
            pass
    if haar is not None:
        try:
            import cv2
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            found = haar.detectMultiScale(
                gray, scaleFactor=1.12, minNeighbors=5,
                minSize=(max(36, width // 24), max(36, height // 24)),
            )
            return [tuple(map(int, item)) for item in found]
        except Exception:
            return []
    return []


def detect_face_track(video_path: Path, start: float, end: float, sample_fps: float = 1.0) -> list[dict]:
    """Sample the selected clip and return normalized face centres.

    OpenCV is optional so rendering still works with a safe centre crop when it
    is unavailable. Detection is bounded to roughly 90 samples per clip.
    """
    try:
        import cv2
    except Exception:
        return []
    yunet = _load_yunet(cv2)
    haar = _load_haar(cv2) if yunet is None else None
    if yunet is None and haar is None:
        return []
    duration = max(0.1, end - start)
    sample_fps = max(0.25, min(sample_fps, 90 / duration))
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        return []
    track: list[dict] = []
    try:
        capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000)
        next_sample = 0.0
        step = 1.0 / sample_fps
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            timestamp = capture.get(cv2.CAP_PROP_POS_MSEC) / 1000.0 - start
            if timestamp < next_sample:
                continue
            if timestamp > duration + 0.25:
                break
            height, width = frame.shape[:2]
            faces = _detect_faces(frame, yunet, haar, width, height)
            if faces:
                x, y, w, h = max(faces, key=lambda item: int(item[2]) * int(item[3]))
                track.append({
                    "time": round(max(0.0, min(duration, timestamp)), 3),
                    "x": round((x + w / 2) / width, 4),
                    "y": round((y + h / 2) / height, 4),
                })
            next_sample += step
    except Exception:
        return track
    finally:
        capture.release()
    return _smooth_track(track, duration)


def _smooth_track(track: list[dict], duration: float) -> list[dict]:
    if not track:
        return []
    sorted_track = sorted(track, key=lambda item: item["time"])
    smoothed: list[dict] = []
    window = 5
    for index, item in enumerate(sorted_track):
        neighbours = sorted_track[max(0, index - window): index + window + 1]
        smoothed.append({
            "time": item["time"],
            "x": round(median(entry["x"] for entry in neighbours), 4),
            "y": round(median(entry["y"] for entry in neighbours), 4),
        })
    if smoothed[0]["time"] > 0.2:
        smoothed.insert(0, {"time": 0.0, "x": smoothed[0]["x"], "y": smoothed[0]["y"]})
    if smoothed[-1]["time"] < duration - 0.2:
        smoothed.append({"time": duration, "x": smoothed[-1]["x"], "y": smoothed[-1]["y"]})
    return smoothed


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _piecewise_expression(points: list[tuple[float, float]]) -> str:
    """Build a bounded piecewise-linear FFmpeg expression from keyframes."""
    if not points:
        return "0"
    if len(points) == 1:
        return f"{points[0][1]:.2f}"
    expression = f"{points[-1][1]:.2f}"
    for index in range(len(points) - 2, -1, -1):
        t0, value0 = points[index]
        t1, value1 = points[index + 1]
        span = max(0.001, t1 - t0)
        segment = f"{value0:.2f}+({value1 - value0:.2f})*(t-{t0:.3f})/{span:.3f}"
        expression = f"if(lt(t,{t1:.3f}),{segment},{expression})"
    return expression


def build_vertical_filter(
    source_width: int,
    source_height: int,
    out_width: int,
    out_height: int,
    *,
    zoom: float = 1.0,
    track: list[dict] | None = None,
    max_keyframes: int = 14,
) -> dict:
    """Return an FFmpeg filter chain and the face-aware caption side."""
    source_width = max(2, int(source_width))
    source_height = max(2, int(source_height))
    zoom = max(1.0, min(1.6, float(zoom)))
    scale = max(out_width / source_width, out_height / source_height) * zoom
    scaled_width = int(round(source_width * scale / 2) * 2)
    scaled_height = int(round(source_height * scale / 2) * 2)
    scaled_width = max(out_width, scaled_width)
    scaled_height = max(out_height, scaled_height)
    max_x = max(0, scaled_width - out_width)
    max_y = max(0, scaled_height - out_height)

    face_side = "bottom"
    x_expression = f"{max_x / 2:.2f}"
    y_expression = f"{max_y / 2:.2f}"
    if track:
        centre_y = sum(item["y"] for item in track) / len(track)
        face_side = "top" if centre_y > 0.52 else "bottom"
        reduced = _reduce_keyframes(track, max_keyframes)
        x_points = [
            (item["time"], _clamp(item["x"] * scaled_width - out_width / 2, 0, max_x))
            for item in reduced
        ]
        y_points = [
            (item["time"], _clamp(item["y"] * scaled_height - out_height / 2, 0, max_y))
            for item in reduced
        ]
        if len({round(value, 1) for _, value in x_points}) > 1:
            x_expression = _piecewise_expression(x_points)
        else:
            x_expression = f"{x_points[0][1]:.2f}"
        if len({round(value, 1) for _, value in y_points}) > 1 and max_y > 0:
            y_expression = _piecewise_expression(y_points)
        else:
            y_expression = f"{y_points[0][1]:.2f}"

    chain = [
        f"scale={scaled_width}:{scaled_height}:flags=lanczos",
        f"crop={out_width}:{out_height}:x='{x_expression}':y='{y_expression}'",
    ]
    return {
        "filter": ",".join(chain),
        "face_detected": bool(track),
        "face_side": face_side,
        "scaled_width": scaled_width,
        "scaled_height": scaled_height,
    }


def _reduce_keyframes(track: list[dict], maximum: int) -> list[dict]:
    if len(track) <= maximum:
        return track
    step = (len(track) - 1) / max(1, maximum - 1)
    indices = sorted({int(round(index * step)) for index in range(maximum)})
    return [track[index] for index in indices]
