"""Common-clock media replay and causal, local camera/audio observations.

The media engine intentionally has no dependency on reference annotations.
AMI observations are derived from the media samples at the current replay time;
the generated feeds use explicit labels so that plumbing tests remain obvious.
"""

from __future__ import annotations

import io
import json
import math
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any

try:
    import cv2
    import numpy as np
except Exception:  # pragma: no cover - exercised only without the optional media stack
    cv2 = None
    np = None

try:
    import soundfile as sf
except Exception:  # pragma: no cover
    sf = None

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = ImageDraw = ImageFont = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "ami_es2002a.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "ami" / "ES2002a"
CAMERA_IDS = ("closeup1", "closeup2", "closeup3", "closeup4", "corner")
FRAME_SIZE = (640, 360)
JPEG_QUALITY = 82
SAMPLE_PERIOD_S = 0.10
BLACK_THRESHOLD = 7.5
BLACK_DEBOUNCE_S = 0.8
SOURCE_STALL_DEBOUNCE_S = 0.8


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded if isinstance(loaded, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _clock_text(time_s: float) -> str:
    total = max(0, int(time_s))
    return f"{total // 60:02d}:{total % 60:02d}"


def _font(size: int, bold: bool = False):
    if ImageFont is None:
        return None
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _jpeg_from_pillow(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=False)
    return buffer.getvalue()


def _offline_card(camera: dict[str, Any], label: str = "CAMERA OFFLINE") -> bytes:
    if Image is None:
        if cv2 is not None and np is not None:
            canvas = np.full((FRAME_SIZE[1], FRAME_SIZE[0], 3), 20, dtype=np.uint8)
            cv2.putText(canvas, label, (130, 190), cv2.FONT_HERSHEY_SIMPLEX, 1, (230, 230, 230), 2)
            return cv2.imencode(".jpg", canvas)[1].tobytes()
        return b""
    image = Image.new("RGB", FRAME_SIZE, (16, 23, 39))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((24, 24, 616, 336), radius=24, fill=(24, 34, 54), outline=(75, 88, 112), width=2)
    draw.rounded_rectangle((48, 48, 592, 90), radius=12, fill=(31, 43, 65))
    draw.text((66, 59), str(camera.get("name", camera["id"])), font=_font(18, True), fill=(219, 229, 242))
    draw.ellipse((263, 118, 377, 232), fill=(58, 33, 40), outline=(239, 102, 119), width=3)
    draw.line((292, 146, 348, 202), fill=(239, 102, 119), width=8)
    draw.line((348, 146, 292, 202), fill=(239, 102, 119), width=8)
    draw.text((190, 255), label, font=_font(22, True), fill=(255, 148, 158))
    draw.text((194, 291), "Noesis source health", font=_font(14), fill=(144, 162, 187))
    return _jpeg_from_pillow(image)


def _black_jpeg() -> bytes:
    if Image is not None:
        return _jpeg_from_pillow(Image.new("RGB", FRAME_SIZE, (0, 0, 0)))
    if cv2 is not None and np is not None:
        return cv2.imencode(".jpg", np.zeros((FRAME_SIZE[1], FRAME_SIZE[0], 3), dtype=np.uint8))[1].tobytes()
    return b""


def _jpeg_luma(jpeg: bytes) -> float:
    """Measure the actual delivered JPEG pixels for black-frame health."""
    if not jpeg:
        return 0.0
    try:
        if Image is not None:
            gray = Image.open(io.BytesIO(jpeg)).convert("L")
            histogram = gray.histogram()
            pixels = max(1, gray.width * gray.height)
            return sum(level * count for level, count in enumerate(histogram)) / pixels
        if cv2 is not None and np is not None:
            decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_GRAYSCALE)
            if decoded is not None:
                return float(np.mean(decoded))
    except Exception:
        pass
    return 0.0


def _synthetic_schedule(time_s: float) -> tuple[list[str], str]:
    """A labeled test schedule. This path is never used for AMI perception."""
    cycle = time_s % 32.0
    if 0 <= cycle < 7:
        return ["A"], "A is speaking"
    if 7 <= cycle < 8.0:
        return ["A", "B"], "Short overlap"
    if 8 <= cycle < 15:
        return ["B"], "B is speaking"
    if 15 <= cycle < 17:
        return ["B", "C"], "Short overlap"
    if 17 <= cycle < 23:
        return ["C"], "C is speaking"
    if 23 <= cycle < 25:
        return ["C", "D"], "Short overlap"
    return ["D"], "D is speaking"


def _synthetic_jpeg(camera: dict[str, Any], time_s: float, speakers: list[str]) -> bytes:
    if Image is None or ImageDraw is None:
        if cv2 is not None and np is not None:
            image = np.zeros((FRAME_SIZE[1], FRAME_SIZE[0], 3), dtype=np.uint8)
            cv2.putText(image, "SYNTHETIC TEST FEED", (25, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.putText(image, camera.get("participant") or "ROOM", (25, 90), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            return cv2.imencode(".jpg", image)[1].tobytes()
        return b""

    participant = camera.get("participant")
    is_speaking = participant in speakers
    color = {
        "A": (103, 191, 238),
        "B": (238, 177, 103),
        "C": (171, 144, 237),
        "D": (108, 214, 171),
        None: (101, 178, 201),
    }.get(participant, (101, 178, 201))
    background = (12, 20, 35)
    image = Image.new("RGB", FRAME_SIZE, background)
    draw = ImageDraw.Draw(image)

    # Low-cost horizontal gradient, rebuilt at the sample rate for a live test feed.
    for y in range(FRAME_SIZE[1]):
        blend = y / max(1, FRAME_SIZE[1] - 1)
        shade = (int(18 + 20 * blend), int(35 + 24 * blend), int(57 + 26 * blend))
        draw.line((0, y, FRAME_SIZE[0], y), fill=shade)
    draw.rounded_rectangle((18, 18, 622, 342), radius=24, fill=(21, 32, 50), outline=(49, 66, 91), width=2)
    draw.rounded_rectangle((34, 32, 606, 76), radius=12, fill=(30, 44, 66))
    draw.ellipse((49, 46, 63, 60), fill=(91, 221, 157) if is_speaking else (113, 133, 160))
    title = f"{camera['name']}  |  PARTICIPANT {participant}" if participant else "CORNER ROOM VIEW  |  FOUR PARTICIPANTS"
    draw.text((75, 43), title, font=_font(17, True), fill=(228, 236, 247))
    draw.text((514, 44), _clock_text(time_s), font=_font(16, True), fill=(165, 186, 212))

    if participant:
        center_x = 320
        pulse = 7 if is_speaking and (int(time_s * 4) % 2 == 0) else 0
        # Attractive stylized person card; deliberately synthetic, not a real face.
        draw.ellipse((center_x - 132, 93 - pulse, center_x + 132, 357 - pulse), fill=(30, 45, 63))
        draw.ellipse((center_x - 92, 101 - pulse, center_x + 92, 285 - pulse), fill=color)
        draw.ellipse((center_x - 68, 126 - pulse, center_x + 68, 262 - pulse), fill=(242, 210, 177))
        # Hair and simple face marks.
        draw.pieslice((center_x - 71, 115 - pulse, center_x + 71, 240 - pulse), 180, 360, fill=(51, 46, 48))
        draw.ellipse((center_x - 34, 188 - pulse, center_x - 24, 197 - pulse), fill=(56, 50, 47))
        draw.ellipse((center_x + 24, 188 - pulse, center_x + 34, 197 - pulse), fill=(56, 50, 47))
        mouth_y = 222 - pulse
        if is_speaking and (int(time_s * 5) % 2 == 0):
            draw.ellipse((center_x - 15, mouth_y - 3, center_x + 15, mouth_y + 13), fill=(133, 57, 67))
        else:
            draw.arc((center_x - 20, mouth_y - 10, center_x + 20, mouth_y + 14), 10, 170, fill=(133, 57, 67), width=3)
        draw.rounded_rectangle((center_x - 111, 287, center_x + 111, 321), radius=16,
                               fill=(25, 36, 52), outline=color, width=2)
        state = "SPEAKING" if is_speaking else "LISTENING"
        draw.text((center_x - 53, 296), state, font=_font(16, True), fill=(244, 248, 252))
    else:
        # A room overview shows the same four synthetic participants at a table.
        draw.rounded_rectangle((81, 123, 559, 283), radius=30, fill=(37, 53, 71), outline=(93, 113, 139), width=2)
        draw.ellipse((142, 105, 222, 185), fill=(103, 191, 238))
        draw.ellipse((238, 91, 318, 171), fill=(238, 177, 103))
        draw.ellipse((334, 91, 414, 171), fill=(171, 144, 237))
        draw.ellipse((430, 105, 510, 185), fill=(108, 214, 171))
        names = [("A", 164), ("B", 260), ("C", 356), ("D", 452)]
        for name, x in names:
            draw.ellipse((x - 16, 130, x - 8, 138), fill=(48, 37, 32))
            draw.ellipse((x + 8, 130, x + 16, 138), fill=(48, 37, 32))
            active = name in speakers
            draw.arc((x - 12, 144, x + 12, 154), 0, 180, fill=(105, 42, 52) if active else (105, 42, 52), width=3)
            draw.text((x - 5, 197), name, font=_font(16, True), fill=(232, 238, 247))
        draw.rounded_rectangle((231, 234, 409, 259), radius=10, fill=(50, 68, 87))
        draw.text((270, 239), "PANEL TABLE", font=_font(12, True), fill=(202, 215, 231))

    draw.rounded_rectangle((34, 317, 606, 333), radius=7, fill=(34, 46, 64))
    draw.text((48, 318), "SYNTHETIC TEST FEED  ·  NOT AMI FOOTAGE", font=_font(11, True), fill=(255, 207, 124))
    if is_speaking:
        draw.rounded_rectangle((462, 282, 583, 308), radius=10, fill=(45, 89, 79))
        draw.text((479, 287), "AUDIO ACTIVE", font=_font(11, True), fill=(152, 239, 192))
    return _jpeg_from_pillow(image)


class MediaEngine:
    """Thread-safe synthetic/AMI replay, frame cache, audio VAD, and fault injection."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir) if data_dir is not None else DEFAULT_DATA_DIR
        if self.data_dir.name != "ES2002a" and (self.data_dir / "ES2002a").is_dir():
            self.data_dir = self.data_dir / "ES2002a"
        self._config = _safe_read_json(DEFAULT_CONFIG) or self._default_config()
        self._lock = threading.RLock()
        self._captures: dict[str, Any] = {}
        self._source_state: dict[str, dict[str, Any]] = {}
        self._audio_handles: dict[str, Any] = {}
        self._audio_state: dict[str, dict[str, Any]] = {}
        self._frame_cache: OrderedDict[str, bytes] = OrderedDict()
        self._camera_info: dict[str, dict[str, Any]] = {}
        self._faults: dict[str, dict[str, Any]] = {}
        self._input_mode = "synthetic"
        self._status = "idle"
        self._base_time = 0.0
        self._anchor_mono = time.monotonic()
        self._duration_s = 150.0
        self._last_sample_time: float | None = None
        self._observation_revision = 0
        self._global_speaker_state = "unknown"
        self._active_speakers: list[str] = []
        self._stream_paths: dict[str, Path] = {}
        self._program_audio_path: Path | None = None
        self._channel_files: dict[str, Path] = {}
        self._using_prepared = False
        self._prepared_manifest: dict[str, Any] | None = None
        self._init_camera_info()

    @staticmethod
    def _default_config() -> dict[str, Any]:
        participants = {"closeup1": ("A", "ES2002a.Closeup1.avi", "ES2002a.Headset-0.wav"),
                        "closeup2": ("C", "ES2002a.Closeup2.avi", "ES2002a.Headset-2.wav"),
                        "closeup3": ("D", "ES2002a.Closeup3.avi", "ES2002a.Headset-3.wav"),
                        "closeup4": ("B", "ES2002a.Closeup4.avi", "ES2002a.Headset-1.wav")}
        cameras = []
        for camera_id in CAMERA_IDS:
            if camera_id == "corner":
                cameras.append({"id": camera_id, "name": "Corner room view", "participant": None,
                                "video_file": "ES2002a.Corner.avi", "audio_file": None})
            else:
                participant, video_file, audio_file = participants[camera_id]
                cameras.append({"id": camera_id, "name": f"Close-up {participant}", "participant": participant,
                                "video_file": video_file, "audio_file": audio_file})
        return {"session": "ES2002a", "credits": "AMI Meeting Corpus ES2002a; CC BY 4.0; synthetic faults and playback modifications.",
                "cameras": cameras, "program_audio_file": "ES2002a.Mix-Headset.wav", "channel_order": list(participants),
                "vad": {"window_s": 0.25, "minimum_confirm_s": 0.35, "hangover_s": 0.45,
                        "minimum_rms": 0.006, "relative_noise_multiplier": 2.8,
                        "cross_talk_ratio": 0.72, "noise_history_s": 12.0},
                "video_offset_s": 0.0, "audio_offset_s": 0.0}

    def _init_camera_info(self) -> None:
        supplied = {str(item.get("id")): item for item in self._config.get("cameras", []) if isinstance(item, dict)}
        for camera_id in CAMERA_IDS:
            item = dict(supplied.get(camera_id) or {})
            item.setdefault("id", camera_id)
            item.setdefault("name", "Corner room view" if camera_id == "corner" else camera_id)
            item.setdefault("participant", None)
            self._camera_info[camera_id] = item

    def _find_prepared_set(self) -> dict[str, Path] | None:
        prepared_root = self.data_dir / "prepared"
        if not prepared_root.is_dir():
            return None
        expected = set()
        for item in self._camera_info.values():
            expected.add(str(item.get("video_file", "")))
            if item.get("audio_file"):
                expected.add(str(item["audio_file"]))
        expected.add(str(self._config.get("program_audio_file", "")))
        candidates = sorted(prepared_root.glob("clip-*/manifest.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        for manifest_path in candidates:
            manifest = _safe_read_json(manifest_path)
            if not manifest or not expected.issubset(set(manifest.get("files", {}))):
                continue
            result: dict[str, Path] = {}
            valid = True
            for original_name in expected:
                relative = manifest["files"].get(original_name, {}).get("relative_output_path")
                if not relative:
                    valid = False
                    break
                path = self.data_dir / Path(relative)
                if not path.is_file():
                    valid = False
                    break
                result[original_name] = path
            if valid:
                self._using_prepared = True
                self._prepared_manifest = manifest
                return result
        return None

    def _resolve_data(self) -> None:
        prepared = self._find_prepared_set()
        result: dict[str, Path] = {}
        for item in self._camera_info.values():
            filename = str(item.get("video_file", ""))
            if filename:
                result[item["id"]] = (prepared or {}).get(filename, self.data_dir / "video" / filename)
        self._stream_paths = result
        audio_root = self.data_dir / "audio"
        self._program_audio_path = ((prepared or {}).get(str(self._config.get("program_audio_file")))
                                    or audio_root / str(self._config.get("program_audio_file", "")))
        self._channel_files = {}
        for item in self._camera_info.values():
            if item.get("participant") and item.get("audio_file"):
                self._channel_files[item["id"]] = ((prepared or {}).get(str(item["audio_file"]))
                                                     or audio_root / str(item["audio_file"]))

    def _discover_duration(self) -> float:
        if self._input_mode == "synthetic":
            return 150.0
        if cv2 is None:
            return 150.0
        durations: list[float] = []
        for path in [*self._stream_paths.values(), *self._channel_files.values(), self._program_audio_path]:
            if path is None or not path.is_file():
                continue
            if path in self._stream_paths.values():
                capture = cv2.VideoCapture(str(path))
                try:
                    frames = capture.get(cv2.CAP_PROP_FRAME_COUNT)
                    fps = capture.get(cv2.CAP_PROP_FPS)
                    if frames > 0 and fps > 0:
                        durations.append(frames / fps)
                finally:
                    capture.release()
            elif sf is not None:
                try:
                    info = sf.info(str(path))
                    if info.frames > 0 and info.samplerate > 0:
                        durations.append(info.frames / info.samplerate)
                except (RuntimeError, OSError):
                    continue
        # The playable common interval includes every available required video,
        # headset, and the continuous program mix. Missing files are reported
        # independently by availability().
        return max(0.1, min(durations)) if durations else 150.0

    def _close_sources(self) -> None:
        for capture in self._captures.values():
            try:
                capture.release()
            except Exception:
                pass
        self._captures.clear()
        for handle in self._audio_handles.values():
            try:
                handle.close()
            except Exception:
                pass
        self._audio_handles.clear()
        self._source_state.clear()
        self._audio_state.clear()

    def start(self, input_mode: str = "synthetic", start_s: float = 0.0) -> dict[str, Any]:
        if input_mode not in {"synthetic", "ami"}:
            raise ValueError("input_mode must be 'synthetic' or 'ami'")
        with self._lock:
            self._close_sources()
            self._input_mode = input_mode
            self._using_prepared = False
            self._prepared_manifest = None
            if input_mode == "ami":
                self._resolve_data()
            self._duration_s = self._discover_duration()
            self._base_time = min(max(0.0, float(start_s)), self._duration_s)
            self._anchor_mono = time.monotonic()
            self._status = "running" if self._base_time < self._duration_s else "stopped"
            self._faults.clear()
            self._invalidate_sample()
            return self.snapshot()

    def pause(self) -> dict[str, Any]:
        with self._lock:
            if self._status == "running":
                self._base_time = self._clock_locked()
                self._status = "paused"
                self._anchor_mono = time.monotonic()
                self._invalidate_sample()
            return self.snapshot()

    def resume(self) -> dict[str, Any]:
        with self._lock:
            if self._status in {"paused", "stopped"} and self._base_time < self._duration_s:
                self._anchor_mono = time.monotonic()
                self._status = "running"
                self._invalidate_sample()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._status = "stopped"
            self._base_time = 0.0
            self._anchor_mono = time.monotonic()
            self._faults.clear()
            self._invalidate_sample()
            return self.snapshot()

    def seek(self, time_s: float) -> dict[str, Any]:
        with self._lock:
            requested = min(max(0.0, float(time_s)), self._duration_s)
            was_running = self._status == "running"
            self._base_time = requested
            self._anchor_mono = time.monotonic()
            self._status = "running" if was_running and requested < self._duration_s else (
                "stopped" if requested >= self._duration_s else "paused"
            )
            self._reset_audio_tracking()
            self._invalidate_sample()
            return self.snapshot()

    def inject_fault(self, camera_id: str, kind: str, duration_s: float = 10.0) -> dict[str, Any]:
        if camera_id not in CAMERA_IDS:
            raise ValueError(f"unknown camera_id {camera_id!r}")
        if kind not in {"black", "freeze", "offline", "none"}:
            raise ValueError("kind must be black, freeze, offline, or none")
        with self._lock:
            if kind == "none":
                self._faults.pop(camera_id, None)
            else:
                frozen_source_time = None
                if kind == "freeze":
                    self._ensure_sample_locked(self._clock_locked(), force=True)
                    frozen = self._frame_cache.get(camera_id)
                    prior = self._camera_info[camera_id].get("_last", {})
                    frozen_source_time = prior.get("source_time_s")
                else:
                    frozen = None
                self._faults[camera_id] = {
                    "kind": kind,
                    "expires_at": time.monotonic() + max(0.1, float(duration_s)),
                    "frozen_jpeg": frozen,
                    "frozen_source_time_s": frozen_source_time,
                }
            self._invalidate_sample()
            return self.snapshot()

    def audio_path(self) -> Path | None:
        with self._lock:
            if self._input_mode != "ami":
                return None
            if self._program_audio_path and self._program_audio_path.is_file():
                return self._program_audio_path
            return None

    def availability(self) -> dict[str, Any]:
        with self._lock:
            missing: list[str] = []
            prepared = self._find_prepared_set()
            for item in self._camera_info.values():
                video_name = str(item.get("video_file", ""))
                video_path = (prepared or {}).get(video_name, self.data_dir / "video" / video_name)
                if video_name and not video_path.is_file():
                    missing.append(f"video/{video_name}")
                audio_name = item.get("audio_file")
                if item.get("participant") and audio_name:
                    audio_path = (prepared or {}).get(str(audio_name), self.data_dir / "audio" / str(audio_name))
                    if not audio_path.is_file():
                        missing.append(f"audio/{audio_name}")
            mix_name = str(self._config.get("program_audio_file", ""))
            mix_path = (prepared or {}).get(mix_name, self.data_dir / "audio" / mix_name)
            if mix_name and not mix_path.is_file():
                missing.append(f"audio/{mix_name}")
            return {
                "ami_available": not missing,
                "missing_files": missing,
                "credits": str(self._config.get("credits", "AMI Meeting Corpus ES2002a; CC BY 4.0.")),
            }

    def _clock_locked(self) -> float:
        if self._status == "running":
            self._base_time = min(self._duration_s, self._base_time + max(0.0, time.monotonic() - self._anchor_mono))
            self._anchor_mono = time.monotonic()
            if self._base_time >= self._duration_s:
                self._status = "stopped"
        return max(0.0, min(self._duration_s, self._base_time))

    def _invalidate_sample(self) -> None:
        self._last_sample_time = None
        self._frame_cache.clear()

    def _reset_audio_tracking(self) -> None:
        for state in self._audio_state.values():
            state["history"].clear()
            state["candidate_since"] = None
            state["last_active"] = None
            state["confirmed"] = False
        self._global_speaker_state = "unknown"
        self._active_speakers = []

    def _fault_now_locked(self) -> None:
        now = time.monotonic()
        expired = [camera_id for camera_id, state in self._faults.items() if now >= state["expires_at"]]
        if expired:
            for camera_id in expired:
                self._faults.pop(camera_id, None)
            self._invalidate_sample()

    def _ensure_sample_locked(self, time_s: float, force: bool = False) -> None:
        self._fault_now_locked()
        if (not force and self._last_sample_time is not None
                and abs(time_s - self._last_sample_time) < SAMPLE_PERIOD_S * 0.75):
            return
        self._observation_revision += 1
        self._frame_cache.clear()
        if self._input_mode == "synthetic":
            self._sample_synthetic_locked(time_s)
        else:
            self._sample_ami_locked(time_s)
        self._last_sample_time = time_s

    def _signal_health_locked(
        self,
        camera_id: str,
        time_s: float,
        jpeg: bytes,
        source_time_s: float | None,
        *,
        arrival_fresh: bool,
        nominal_quality: float,
    ) -> tuple[bool, str, float, float]:
        """Health derives from delivered pixels, source PTS and frame arrival."""
        now = time.monotonic()
        state = self._source_state.setdefault(camera_id, {
            "last_arrival_mono": now,
            "health_clock_s": None,
            "health_source_time_s": None,
            "health_stall_since_s": None,
            "black_since_mono": None,
        })
        if arrival_fresh:
            state["last_arrival_mono"] = now
        previous_clock = state.get("health_clock_s")
        previous_source = state.get("health_source_time_s")
        if (source_time_s is not None and previous_clock is not None and previous_source is not None
                and time_s > float(previous_clock) + 0.001):
            if source_time_s <= float(previous_source) + 0.001:
                if state.get("health_stall_since_s") is None:
                    state["health_stall_since_s"] = float(previous_clock)
            else:
                state["health_stall_since_s"] = None
        if source_time_s is not None:
            state["health_clock_s"] = time_s
            state["health_source_time_s"] = source_time_s

        age_ms = max(0.0, (time_s - source_time_s) * 1000.0) if source_time_s is not None else 0.0
        luma = _jpeg_luma(jpeg)
        if luma < BLACK_THRESHOLD:
            if state.get("black_since_mono") is None:
                state["black_since_mono"] = now
        else:
            state["black_since_mono"] = None

        arrival_age = max(0.0, now - float(state.get("last_arrival_mono", now)))
        stall_since = state.get("health_stall_since_s")
        stalled = stall_since is not None and time_s - float(stall_since) >= SOURCE_STALL_DEBOUNCE_S
        if arrival_age >= SOURCE_STALL_DEBOUNCE_S:
            return False, "offline", 0.0, age_ms
        if stalled:
            return False, "stalled", min(nominal_quality, 0.15), age_ms
        if (state.get("black_since_mono") is not None
                and now - float(state["black_since_mono"]) >= BLACK_DEBOUNCE_S):
            return False, "black", min(nominal_quality, 0.05), age_ms
        return True, "online", nominal_quality, age_ms

    def _sample_synthetic_locked(self, time_s: float) -> None:
        speakers, description = _synthetic_schedule(time_s)
        self._active_speakers = speakers
        self._global_speaker_state = "overlap" if len(speakers) > 1 else ("speaker" if speakers else "silence")
        for camera_id in CAMERA_IDS:
            camera = self._camera_info[camera_id]
            participant = camera.get("participant")
            jpeg = _synthetic_jpeg(camera, time_s, speakers)
            fault = self._faults.get(camera_id)
            jpeg = self._apply_fault_locked(camera_id, jpeg, fault)
            source_state = self._source_state.setdefault(camera_id, {
                "last_arrival_mono": time.monotonic(),
                "health_source_time_s": time_s,
            })
            arrival_fresh = True
            source_time = time_s
            if fault and fault["kind"] == "freeze":
                source_time = float(fault.get("frozen_source_time_s")
                                    if fault.get("frozen_source_time_s") is not None
                                    else source_state.get("health_source_time_s", time_s))
            elif fault and fault["kind"] == "offline":
                source_time = float(source_state.get("health_source_time_s", time_s))
                arrival_fresh = False
            speaking: bool | None = participant in speakers if participant else False
            if len(speakers) > 1 and participant in speakers:
                speaking = None
            active = participant in speakers
            energy = 0.42 + 0.08 * math.sin(time_s * 3.0) if active else 0.015
            healthy, status, quality, age_ms = self._signal_health_locked(
                camera_id, time_s, jpeg, source_time,
                arrival_fresh=arrival_fresh,
                nominal_quality=0.96,
            )
            self._frame_cache[camera_id] = jpeg
            self._camera_info[camera_id]["_last"] = {
                "healthy": healthy,
                "status": "synthetic" if healthy else status,
                "speaking": speaking,
                "speaker_state": "overlap" if len(speakers) > 1 and active else ("speaking" if active else "silence"),
                "energy": max(0.0, min(1.0, energy)),
                "quality": quality,
                "age_ms": age_ms,
                "source_time_s": source_time,
                "reason": description,
            }

    def _open_capture(self, camera_id: str, path: Path) -> tuple[Any, dict[str, Any]] | None:
        if cv2 is None or not path.is_file():
            return None
        capture = self._captures.get(camera_id)
        if capture is None:
            capture = cv2.VideoCapture(str(path))
            if not capture.isOpened():
                capture.release()
                return None
            self._captures[camera_id] = capture
            self._source_state[camera_id] = {
                "fps": max(0.1, float(capture.get(cv2.CAP_PROP_FPS) or 25.0)),
                "frame_count": int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0),
                "last_requested_idx": None,
                "last_decoded_idx": None,
                "stall_since": None,
                "black_since": None,
                "last_arrival_mono": time.monotonic(),
                "last_source_time": None,
                "positioned": False,
            }
        return capture, self._source_state[camera_id]

    def _decode_frame(self, camera_id: str, path: Path, target_time_s: float) -> tuple[Any, float, float] | None:
        opened = self._open_capture(camera_id, path)
        if opened is None or cv2 is None:
            return None
        capture, state = opened
        fps = state["fps"]
        offset = float(self._config.get("video_offset_s", 0.0))
        source_t = max(0.0, target_time_s + offset)
        target_idx = max(0, int(math.floor(source_t * fps + 1e-7)))
        if target_idx == state.get("last_decoded_idx") and state.get("last_frame") is not None:
            # Low-FPS feeds can present the same source frame at consecutive
            # clock samples. Reuse that one decoded frame while still
            # refreshing audio and the rest of the synchronized snapshot.
            return (state["last_frame"], float(state["last_source_time"]),
                    float(state["last_arrival_mono"]))
        current_position = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))))
        last_request = state["last_requested_idx"]
        if (not state["positioned"] or last_request is None or target_idx < last_request
                or target_idx < current_position - 1 or target_idx - current_position > max(100, int(fps * 5))):
            if not capture.set(cv2.CAP_PROP_POS_FRAMES, target_idx):
                return None
            state["positioned"] = True
            current_position = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))))

        frame = None
        decoded_idx = None
        # Sequentially consume intervening frames. This preserves source
        # progression across ordinary 100 ms samples and avoids one decoder
        # seek for every browser frame request.
        attempts = 0
        while current_position <= target_idx and attempts < 300:
            before = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))))
            ok, candidate = capture.read()
            if not ok or candidate is None:
                return None
            after = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))))
            actual_idx = max(0, after - 1) if after > before else before
            if actual_idx == target_idx:
                frame = candidate
                decoded_idx = actual_idx
                break
            if actual_idx > target_idx:
                # A decoder that advanced past the requested PTS cannot be
                # used causally. Retry via explicit frame seek and reject if
                # the backend still returns a future frame.
                capture.set(cv2.CAP_PROP_POS_FRAMES, target_idx)
                ok, candidate = capture.read()
                if not ok or candidate is None:
                    return None
                after = max(0, int(round(capture.get(cv2.CAP_PROP_POS_FRAMES))))
                actual_idx = max(0, after - 1)
                if actual_idx > target_idx:
                    return None
                frame, decoded_idx = candidate, actual_idx
                break
            current_position = after if after > before else before + 1
            attempts += 1
        if frame is None or decoded_idx is None:
            return None

        arrival = time.monotonic()
        if last_request is not None and target_idx > last_request:
            previous_decoded = state["last_decoded_idx"]
            if previous_decoded is not None and decoded_idx <= previous_decoded:
                if state["stall_since"] is None:
                    state["stall_since"] = arrival
            else:
                state["stall_since"] = None
        elif last_request is not None and target_idx < last_request:
            state["stall_since"] = None
        state["last_requested_idx"] = target_idx
        state["last_decoded_idx"] = decoded_idx
        state["last_arrival_mono"] = arrival
        actual_source_t = decoded_idx / fps
        actual_media_t = actual_source_t - offset
        state["last_source_time"] = actual_media_t
        if actual_media_t > target_time_s + 0.001:
            return None
        state["last_frame"] = frame
        return frame, max(0.0, actual_media_t), arrival

    def _audio_handle(self, camera_id: str, path: Path):
        if sf is None or not path.is_file():
            return None
        handle = self._audio_handles.get(camera_id)
        if handle is None:
            try:
                handle = sf.SoundFile(str(path), mode="r")
            except (RuntimeError, OSError):
                return None
            self._audio_handles[camera_id] = handle
            self._audio_state[camera_id] = {
                "history": deque(maxlen=max(8, int(12.0 / 0.25))),
                "candidate_since": None,
                "last_active": None,
                "confirmed": False,
                "energy": 0.0,
                "speaking": False,
            }
        return handle

    def _read_energy_locked(self, camera_id: str, time_s: float, path: Path) -> float | None:
        handle = self._audio_handle(camera_id, path)
        if handle is None:
            return None
        vad = self._config.get("vad", {})
        window_s = float(vad.get("window_s", 0.25))
        offset = float(self._config.get("audio_offset_s", 0.0))
        sample_rate = int(handle.samplerate)
        channel_time = time_s + offset
        end_frame = int(max(0.0, channel_time) * sample_rate)
        frames = max(1, int(window_s * sample_rate))
        start_frame = max(0, end_frame - frames)
        if start_frame >= int(handle.frames):
            return None
        read_count = min(frames, int(handle.frames) - start_frame)
        if read_count <= 0:
            return None
        try:
            handle.seek(start_frame)
            data = handle.read(read_count, dtype="float32", always_2d=True)
        except (RuntimeError, OSError, ValueError):
            return None
        if np is None or data.size == 0:
            return None
        # Audio input is an isolated mono channel in this fixture. If a file
        # has several channels, average them without looking beyond this time.
        mono = np.mean(data, axis=1, dtype=np.float64)
        return float(np.sqrt(np.mean(np.square(mono), dtype=np.float64)))

    def _perceive_audio_locked(self, time_s: float) -> dict[str, dict[str, Any]]:
        vad = self._config.get("vad", {})
        minimum_rms = float(vad.get("minimum_rms", 0.006))
        noise_multiplier = float(vad.get("relative_noise_multiplier", 2.8))
        confirmation = float(vad.get("minimum_confirm_s", 0.35))
        hangover = float(vad.get("hangover_s", 0.45))
        overlap_ratio = float(vad.get("cross_talk_ratio", 0.72))
        history_s = float(vad.get("noise_history_s", 12.0))
        window_s = float(vad.get("window_s", 0.25))
        for state in self._audio_state.values():
            maxlen = max(8, int(math.ceil(history_s / max(0.05, window_s))))
            if state["history"].maxlen != maxlen:
                state["history"] = deque(state["history"], maxlen=maxlen)

        results: dict[str, dict[str, Any]] = {}
        candidates: list[tuple[str, float]] = []
        available = 0
        for camera_id, path in self._channel_files.items():
            energy = self._read_energy_locked(camera_id, time_s, path)
            state = self._audio_state.get(camera_id)
            if state is None:
                results[camera_id] = {"energy": None, "speaking": None, "state": "unknown"}
                continue
            if energy is None:
                state["confirmed"] = False
                state["candidate_since"] = None
                results[camera_id] = {"energy": None, "speaking": None, "state": "unknown"}
                continue
            available += 1
            state["energy"] = energy
            prior = list(state["history"])
            floor = max(0.0005, float(np.percentile(prior, 25)) if prior and np is not None else 0.0005)
            threshold = max(minimum_rms, floor * noise_multiplier)
            raw_active = energy >= threshold
            # Keep an online lower-envelope noise estimate from samples that
            # were already below the causal speech threshold. A clip that
            # starts mid-sentence therefore cannot calibrate speech into its
            # own noise floor. The ring is bounded and annotation-free.
            if not raw_active:
                state["history"].append(energy)
            if raw_active:
                if state["candidate_since"] is None:
                    state["candidate_since"] = time_s
                if time_s - float(state["candidate_since"]) >= confirmation:
                    state["confirmed"] = True
                if state["confirmed"]:
                    state["last_active"] = time_s
            else:
                state["candidate_since"] = None
                if state["last_active"] is not None and time_s - float(state["last_active"]) > hangover:
                    state["confirmed"] = False
            if state["confirmed"]:
                candidates.append((camera_id, energy))
            results[camera_id] = {
                "energy": energy,
                "speaking": bool(state["confirmed"]),
                "state": "speaking" if state["confirmed"] else "silence",
                "threshold": threshold,
            }

        candidates.sort(key=lambda item: item[1], reverse=True)
        if available == 0:
            global_state = "unknown"
            active_ids: list[str] = []
        elif not candidates:
            global_state = "silence"
            active_ids = []
        elif len(candidates) > 1 and candidates[1][1] >= max(1e-8, candidates[0][1] * overlap_ratio):
            global_state = "overlap"
            active_ids = [camera_id for camera_id, _ in candidates]
        elif len(candidates) > 1:
            # A much quieter secondary channel is treated as cross-talk; only
            # the dominant, sustained channel remains a speaker candidate.
            global_state = "speaker"
            active_ids = [candidates[0][0]]
        else:
            global_state = "speaker"
            active_ids = [candidates[0][0]]

        self._global_speaker_state = global_state
        self._active_speakers = [str(self._camera_info[camera_id].get("participant")) for camera_id in active_ids]
        for camera_id, result in results.items():
            if global_state == "unknown":
                result["speaking"] = None
                result["state"] = "unknown"
            elif global_state == "overlap":
                result["speaking"] = None if camera_id in active_ids else False
                result["state"] = "overlap" if camera_id in active_ids else "silence"
            else:
                result["speaking"] = camera_id in active_ids
                result["state"] = "speaking" if camera_id in active_ids else "silence"
        return results

    @staticmethod
    def _quality(frame: Any) -> tuple[float, float]:
        if cv2 is None or np is None or frame is None or getattr(frame, "size", 0) == 0:
            return 0.0, 0.0
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        mean = float(np.mean(gray))
        brightness = min(1.0, mean / 75.0) * min(1.0, max(0.0, 255.0 - mean) / 55.0)
        return mean, max(0.05, min(0.98, brightness))

    def _apply_fault_locked(self, camera_id: str, jpeg: bytes, fault: dict[str, Any] | None) -> bytes:
        if fault is None:
            return jpeg
        kind = fault["kind"]
        if kind == "black":
            return _black_jpeg()
        if kind == "freeze":
            return fault.get("frozen_jpeg") or jpeg
        if kind == "offline":
            return _offline_card(self._camera_info[camera_id], "SIGNAL LOST")
        return jpeg

    def _sample_ami_locked(self, time_s: float) -> None:
        if cv2 is None or np is None:
            for camera_id in CAMERA_IDS:
                jpeg = _offline_card(self._camera_info[camera_id], "OPENCV UNAVAILABLE")
                jpeg = self._apply_fault_locked(camera_id, jpeg, self._faults.get(camera_id))
                self._frame_cache[camera_id] = jpeg
                self._camera_info[camera_id]["_last"] = {
                    "healthy": False, "status": "decoder-unavailable", "speaking": None,
                    "speaker_state": "unknown", "energy": None, "quality": 0.0, "age_ms": 0.0,
                    "source_time_s": None,
                }
            self._global_speaker_state, self._active_speakers = "unknown", []
            return

        audio_observations = self._perceive_audio_locked(time_s)
        for camera_id in CAMERA_IDS:
            camera = self._camera_info[camera_id]
            path = self._stream_paths.get(camera_id)
            record = {
                "healthy": False,
                "status": "missing" if path is None or not path.is_file() else "offline",
                "speaking": audio_observations.get(camera_id, {}).get("speaking"),
                "speaker_state": audio_observations.get(camera_id, {}).get("state", "unknown"),
                "energy": audio_observations.get(camera_id, {}).get("energy"),
                "quality": 0.0,
                "age_ms": 0.0,
                "source_time_s": None,
            }
            fault = self._faults.get(camera_id)
            if path is None or not path.is_file():
                jpeg = _offline_card(camera, "AMI FILE MISSING")
                record["status"] = "missing"
                record["healthy"] = False
            elif fault and fault["kind"] == "offline":
                # No decoded frame arrives from a disconnected source. The
                # health path sees the old PTS and arrival age only; it does
                # not receive the requested fault kind.
                state = self._source_state.get(camera_id, {})
                last_record = camera.get("_last", {})
                source_time = last_record.get("source_time_s")
                if source_time is None:
                    source_time = state.get("last_source_time")
                if source_time is None:
                    source_time = time_s
                jpeg = _offline_card(camera, "SIGNAL LOST")
                healthy, status, quality, age_ms = self._signal_health_locked(
                    camera_id, time_s, jpeg, float(source_time),
                    arrival_fresh=False,
                    nominal_quality=float(last_record.get("quality", 0.6)),
                )
                record.update({"healthy": healthy, "status": status, "quality": quality,
                               "source_time_s": float(source_time), "age_ms": age_ms})
            elif fault and fault["kind"] == "freeze":
                # A frozen source keeps delivering its last frame, but its
                # source timestamp no longer advances. Repeated arrivals
                # prevent this from being mislabeled as a disconnected feed.
                state = self._source_state.get(camera_id, {})
                last_record = camera.get("_last", {})
                source_time = fault.get("frozen_source_time_s")
                if source_time is None:
                    source_time = last_record.get("source_time_s", state.get("last_source_time", time_s))
                jpeg = self._apply_fault_locked(
                    camera_id,
                    last_record.get("_source_jpeg", last_record.get("_jpeg", b"")) or _offline_card(camera, "FRAME STALLED"),
                    fault,
                )
                if not jpeg:
                    jpeg = fault.get("frozen_jpeg") or _offline_card(camera, "FRAME STALLED")
                healthy, status, quality, age_ms = self._signal_health_locked(
                    camera_id, time_s, jpeg, float(source_time),
                    arrival_fresh=True,
                    nominal_quality=float(last_record.get("quality", 0.6)),
                )
                record.update({"healthy": healthy, "status": status, "quality": quality,
                               "source_time_s": float(source_time), "age_ms": age_ms})
            else:
                decoded = self._decode_frame(camera_id, path, time_s)
                if decoded is None:
                    jpeg = _offline_card(camera, "DECODE ERROR")
                    record["status"] = "decode-error"
                    record["healthy"] = False
                else:
                    frame, source_time, _arrival = decoded
                    if fault and fault["kind"] == "black":
                        frame = np.zeros_like(frame)
                    mean_luma, quality = self._quality(frame)
                    encode_ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
                    jpeg = encoded.tobytes() if encode_ok else _offline_card(camera, "ENCODE ERROR")
                    healthy, status, quality, age_ms = self._signal_health_locked(
                        camera_id, time_s, jpeg, source_time,
                        arrival_fresh=True,
                        nominal_quality=quality,
                    )
                    record.update({"healthy": healthy, "status": status, "quality": quality,
                                   "source_time_s": source_time, "age_ms": age_ms})
                    state = self._source_state[camera_id]
                    if state.get("stall_since") is not None and time.monotonic() - float(state["stall_since"]) >= SOURCE_STALL_DEBOUNCE_S:
                        record.update({"healthy": False, "status": "stalled", "quality": min(quality, 0.15)})
            # `jpeg` is the only frame delivered to preview/program and is the
            # same signal measured above. Fault metadata never directly sets a
            # camera's health or status.
            self._frame_cache[camera_id] = jpeg
            self._camera_info[camera_id]["_last"] = record
            if jpeg:
                self._camera_info[camera_id]["_last"]["_jpeg"] = jpeg
                self._camera_info[camera_id]["_last"]["_source_jpeg"] = jpeg

    def _camera_snapshot_locked(self, camera_id: str) -> dict[str, Any]:
        camera = self._camera_info[camera_id]
        last = camera.get("_last", {})
        return {
            "id": camera_id,
            "name": camera.get("name", camera_id),
            "participant": camera.get("participant"),
            "healthy": bool(last.get("healthy", False)),
            "status": str(last.get("status", "initializing")),
            "speaking": last.get("speaking"),
            "speaker_state": str(last.get("speaker_state", "unknown")),
            "energy": last.get("energy"),
            "quality": float(last.get("quality", 0.0)),
            "age_ms": float(last.get("age_ms", 0.0)),
            "source_time_s": last.get("source_time_s"),
            "frame_url": f"/api/frame/{camera_id}.jpg?rev={self._observation_revision}",
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            time_s = self._clock_locked()
            self._ensure_sample_locked(time_s)
            return {
                "time_s": time_s,
                "duration_s": self._duration_s,
                "input_mode": self._input_mode,
                "status": self._status,
                "speaker_state": self._global_speaker_state,
                "active_speakers": list(self._active_speakers),
                "prepared_clip": bool(self._using_prepared),
                "cameras": [self._camera_snapshot_locked(camera_id) for camera_id in CAMERA_IDS],
                "observation_revision": self._observation_revision,
            }

    def frame_jpeg(self, camera_id: str) -> bytes:
        if camera_id not in CAMERA_IDS:
            raise ValueError(f"unknown camera_id {camera_id!r}")
        with self._lock:
            time_s = self._clock_locked()
            self._ensure_sample_locked(time_s)
            return bytes(self._frame_cache.get(camera_id, _offline_card(self._camera_info[camera_id])))


__all__ = ["MediaEngine", "CAMERA_IDS"]
