"""Bounded background ASR and frame measurements for causal editorial context.

This module never downloads a model and never reads annotation files. Worker
results are tagged with the session and epoch that scheduled them; stale work
is discarded before it can enter a published snapshot.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
from pathlib import Path
import threading
import time
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = PROJECT_ROOT / ".runtime" / "models" / "whisper-tiny.en"
CAMERA_IDS = ("closeup1", "closeup2", "closeup3", "closeup4", "corner")
ASR_CHUNK_S = 4.0
VISUAL_INTERVAL_S = 1.0
MAX_TRANSCRIPT_SEGMENTS = 6
MAX_TRANSCRIPT_CHARS = 600
MAX_VISUAL_OBSERVATIONS = 25
TARGET_SAMPLE_RATE = 16_000


@dataclass(frozen=True)
class _Job:
    generation: int
    session_id: str
    epoch: int
    source_start_s: float
    source_end_s: float
    payload: Any


@dataclass(frozen=True)
class _Transcript:
    session_id: str
    epoch: int
    text: str
    source_start_s: float
    source_end_s: float
    completed_mono: float


@dataclass(frozen=True)
class _Visual:
    session_id: str
    epoch: int
    camera_id: str
    source_time_s: float
    frame_time_s: float
    blur_score: float | None
    luma_mean: float | None
    face_count: int | None
    face_status: str
    completed_mono: float


class _LatestWorker:
    """A single daemon worker with one replaceable pending job."""

    def __init__(self, owner: "BackgroundPerception", modality: str, process: Callable[[_Job], Any]):
        self.owner = owner
        self.modality = modality
        self.process = process
        self.condition = threading.Condition()
        self.pending: _Job | None = None
        self.thread: threading.Thread | None = None
        self.closed = False
        self.replaced = 0

    def submit(self, job: _Job) -> None:
        # Serialize submit with epoch invalidation: a stale observer must not
        # replace a newer epoch's pending job after reset has cleared the slot.
        with self.owner._lock:
            if self.owner._closed or job.generation != self.owner._generation or (
                job.session_id, job.epoch
            ) != (self.owner._session_id, self.owner._epoch):
                return
            with self.condition:
                if self.closed:
                    return
                if self.pending is not None:
                    self.replaced += 1
                self.pending = job
                if self.thread is None:
                    self.thread = threading.Thread(
                        target=self._run,
                        name=f"noesis-perception-{self.modality}",
                        daemon=True,
                    )
                    self.thread.start()
                self.condition.notify()

    def clear_pending(self, *, reset_replaced: bool = False) -> None:
        with self.condition:
            self.pending = None
            if reset_replaced:
                self.replaced = 0

    def close(self, timeout_s: float) -> None:
        with self.condition:
            self.closed = True
            self.pending = None
            self.condition.notify_all()
            thread = self.thread
        if thread is not None:
            thread.join(max(0.0, timeout_s))

    def _run(self) -> None:
        while True:
            with self.condition:
                while self.pending is None and not self.closed:
                    self.condition.wait()
                if self.closed:
                    return
                job = self.pending
                self.pending = None
            if job is None or not self.owner._is_current(job):
                continue
            started = time.monotonic()
            self.owner._worker_started(self.modality, job)
            try:
                result = self.process(job)
            except Exception as exc:  # Keep worker failures private and recoverable.
                self.owner._worker_failed(self.modality, job, exc, (time.monotonic() - started) * 1000)
            else:
                self.owner._worker_completed(
                    self.modality, job, result, (time.monotonic() - started) * 1000
                )


class BackgroundPerception:
    """Asynchronously transcribe causal AMI chunks and measure captured JPEGs.

    ``model_dir`` is the exact local faster-whisper model directory, not its
    parent. No model import or download occurs until the speech worker starts.
    Inject ``asr_factory`` and ``visual_processor`` for deterministic tests.
    """

    def __init__(
        self,
        media: Any,
        model_dir: Path | str = DEFAULT_MODEL_DIR,
        *,
        speech_enabled: bool = True,
        visual_enabled: bool = True,
        asr_factory: Callable[[Path], Any] | None = None,
        visual_processor: Callable[[bytes], dict[str, Any]] | None = None,
        chunk_s: float = ASR_CHUNK_S,
        visual_interval_s: float = VISUAL_INTERVAL_S,
    ):
        self.media = media
        self.model_dir = Path(model_dir)
        self.speech_enabled = bool(speech_enabled)
        self.visual_enabled = bool(visual_enabled)
        self.asr_factory = asr_factory
        self.visual_processor = visual_processor
        self.chunk_s = min(ASR_CHUNK_S, max(0.5, float(chunk_s)))
        self.visual_interval_s = max(0.25, float(visual_interval_s))

        self._lock = threading.RLock()
        self._closed = False
        self._generation = 0
        self._session_id = ""
        self._epoch = 0
        self._input_mode = "unknown"
        self._speech_origin_s: float | None = None
        self._speech_next_end_s: float | None = None
        self._speech_last_end_s: float | None = None
        self._speech_eof_scheduled = False
        self._last_visual_submit_s: float | None = None
        self._asr_model: Any = None
        self._visual_detector: Any = None
        self._transcripts: deque[_Transcript] = deque(maxlen=MAX_TRANSCRIPT_SEGMENTS)
        self._visuals: deque[_Visual] = deque(maxlen=MAX_VISUAL_OBSERVATIONS)
        self._status: dict[str, dict[str, Any]] = {
            "speech": self._initial_status("speech"),
            "visual": self._initial_status("visual"),
        }
        self._speech_worker = _LatestWorker(self, "speech", self._process_speech)
        self._visual_worker = _LatestWorker(self, "visual", self._process_visual)

    def _initial_status(self, modality: str) -> dict[str, Any]:
        enabled = self.speech_enabled if modality == "speech" else self.visual_enabled
        if not enabled:
            return self._status_value("disabled")
        if modality == "speech" and self.asr_factory is None and not self._model_files_present():
            return self._status_value("unavailable", "ModelFilesMissing")
        return self._status_value("idle")

    @staticmethod
    def _status_value(state: str, error_class: str | None = None) -> dict[str, Any]:
        return {
            "state": state,
            "latency_ms": None,
            "last_error_class": error_class,
            "last_source_end_s": None,
            "queue_replaced": 0,
        }

    def _model_files_present(self) -> bool:
        return self.model_dir.is_dir() and (self.model_dir / "model.bin").is_file()

    def reset(self, session_id: str, epoch: int) -> None:
        """Invalidate in-flight work and clear published context for a new epoch."""
        with self._lock:
            if self._closed:
                return
            self._generation += 1
            self._session_id = str(session_id)
            self._epoch = int(epoch)
            self._input_mode = "unknown"
            self._speech_origin_s = None
            self._speech_next_end_s = None
            self._speech_last_end_s = None
            self._speech_eof_scheduled = False
            self._last_visual_submit_s = None
            self._transcripts.clear()
            self._visuals.clear()
            self._status = {
                "speech": self._initial_status("speech"),
                "visual": self._initial_status("visual"),
            }
        self._speech_worker.clear_pending(reset_replaced=True)
        self._visual_worker.clear_pending(reset_replaced=True)

    def observe(self, snapshot: dict[str, Any], media: Any, session_id: str, epoch: int) -> None:
        """Schedule latest-only jobs; does no inference, file decoding, or download."""
        if not isinstance(snapshot, dict) or self._closed:
            return
        try:
            media_time = float(snapshot.get("time_s"))
        except (TypeError, ValueError):
            return
        if not math.isfinite(media_time) or media_time < 0:
            return
        identity = (str(session_id), int(epoch))
        speech_job: _Job | None = None
        visual_job: _Job | None = None
        with self._lock:
            if self._closed:
                return
            if identity != (self._session_id, self._epoch):
                self._generation += 1
                self._session_id, self._epoch = identity
                self._transcripts.clear()
                self._visuals.clear()
                duration = _optional_finite(snapshot.get("duration_s"))
                first_seen_at_eof = duration is not None and duration > 0 and media_time >= duration - 0.025
                speech_origin = max(0.0, media_time - self.chunk_s) if first_seen_at_eof else media_time
                self._speech_origin_s = speech_origin
                self._speech_next_end_s = speech_origin + self.chunk_s
                self._speech_last_end_s = speech_origin
                self._speech_eof_scheduled = False
                self._last_visual_submit_s = None
                self._status = {
                    "speech": self._initial_status("speech"),
                    "visual": self._initial_status("visual"),
                }
                generation = self._generation
                clear_workers = True
            else:
                generation = self._generation
                clear_workers = False
            self._input_mode = str(snapshot.get("input_mode", "unknown"))
            input_mode = self._input_mode
            current_session, current_epoch = self._session_id, self._epoch

            if self._input_mode == "synthetic":
                self._status["speech"] = self._status_value("skipped_synthetic")
                self._status["visual"] = self._status_value("skipped_synthetic")
            elif self._input_mode == "ami":
                if self.speech_enabled and self._status["speech"]["state"] != "unavailable":
                    duration = _optional_finite(snapshot.get("duration_s"))
                    at_eof = (
                        duration is not None and duration > 0
                        and media_time >= duration - 0.025
                    )
                    next_end = self._speech_next_end_s
                    if at_eof and not self._speech_eof_scheduled:
                        end_s = min(media_time, duration)
                        previous_end = self._speech_last_end_s
                        origin = float(self._speech_origin_s or 0.0)
                        start_s = max(origin, end_s - self.chunk_s, float(previous_end if previous_end is not None else origin))
                        self._speech_eof_scheduled = True
                        self._speech_last_end_s = end_s
                        if end_s - start_s < 0.05:
                            speech_job = None
                        else:
                            speech_job = self._make_speech_job(media, generation, current_session, current_epoch, start_s, end_s)
                    elif next_end is None:
                        self._speech_origin_s = media_time
                        self._speech_next_end_s = media_time + self.chunk_s
                        self._speech_last_end_s = media_time
                    elif not at_eof and media_time + 1e-6 >= next_end:
                        jumps = max(1, int((media_time - next_end) // self.chunk_s) + 1)
                        end_s = float(next_end + (jumps - 1) * self.chunk_s)
                        origin = float(self._speech_origin_s or 0.0)
                        start_s = max(origin, end_s - self.chunk_s)
                        self._speech_next_end_s = end_s + self.chunk_s
                        self._speech_last_end_s = end_s
                        speech_job = self._make_speech_job(media, generation, current_session, current_epoch, start_s, end_s)

                visual_job: _Job | None = None
                if self.visual_enabled and (
                    self._last_visual_submit_s is None
                    or media_time - self._last_visual_submit_s + 1e-6 >= self.visual_interval_s
                ):
                    self._last_visual_submit_s = media_time
                    visual_job = _Job(
                        generation, current_session, current_epoch, media_time, media_time,
                        {"media": media, "camera_ids": CAMERA_IDS},
                    )
            else:
                speech_job = None
                visual_job = None

        if clear_workers:
            self._speech_worker.clear_pending(reset_replaced=True)
            self._visual_worker.clear_pending(reset_replaced=True)
        if input_mode != "ami":
            return
        if speech_job is not None:
            self._mark_queued("speech", speech_job)
            self._speech_worker.submit(speech_job)
        if visual_job is not None:
            self._mark_queued("visual", visual_job)
            self._visual_worker.submit(visual_job)

    def _make_speech_job(self, media: Any, generation: int, session_id: str, epoch: int,
                         start_s: float, end_s: float) -> _Job:
        audio_path = None
        try:
            path_getter = getattr(media, "audio_path", None)
            audio_path = path_getter() if callable(path_getter) else None
        except Exception:
            audio_path = None
        return _Job(
            generation, session_id, epoch, start_s, end_s,
            {"audio_path": Path(audio_path) if audio_path else None},
        )

    def snapshot(
        self,
        session_id: str,
        epoch: int,
        source_end_s: float | None = None,
        *,
        target_time_s: float | None = None,
    ) -> dict[str, Any]:
        """Return a bounded JSON-safe copy of only current, causal worker results."""
        if source_end_s is None:
            source_end_s = target_time_s
        try:
            cutoff = max(0.0, float(source_end_s))
        except (TypeError, ValueError):
            cutoff = 0.0
        if not math.isfinite(cutoff):
            cutoff = 0.0
        requested_session, requested_epoch = str(session_id), int(epoch)
        now = time.monotonic()
        with self._lock:
            matches = (requested_session, requested_epoch) == (self._session_id, self._epoch)
            transcripts = [
                item for item in self._transcripts
                if matches and item.session_id == requested_session and item.epoch == requested_epoch
                and item.source_end_s <= cutoff + 1e-6
            ]
            visuals = [
                item for item in self._visuals
                if matches and item.session_id == requested_session and item.epoch == requested_epoch
                and item.source_time_s <= cutoff + 1e-6
                and item.frame_time_s <= cutoff + 1e-6
            ]
            status = {key: dict(value) for key, value in self._status.items()}
            input_mode = self._input_mode
        selected_segments: list[dict[str, Any]] = []
        remaining_chars = MAX_TRANSCRIPT_CHARS
        for item in reversed(transcripts):
            text = item.text[:remaining_chars]
            if not text:
                break
            remaining_chars -= len(text)
            selected_segments.append({
                "text": text,
                "source_start_s": item.source_start_s,
                "source_end_s": item.source_end_s,
                "age_ms": max(0.0, (cutoff - item.source_end_s) * 1000.0),
                "speaker_id": None,
                "untrusted": True,
            })
        segments = list(reversed(selected_segments))
        visual_observations = [
            {
                "camera_id": item.camera_id,
                "source_time_s": item.source_time_s,
                "frame_time_s": item.frame_time_s,
                "age_ms": max(0.0, (cutoff - item.source_time_s) * 1000.0),
                "blur_score": item.blur_score,
                "luma_mean": item.luma_mean,
                "face_count": item.face_count,
                "face_status": item.face_status,
            }
            for item in visuals
        ]
        transcript_text = " ".join(item["text"] for item in segments)[:MAX_TRANSCRIPT_CHARS]
        return {
            "session_id": requested_session,
            "epoch": requested_epoch,
            "source_end_s": cutoff,
            "input_mode": input_mode if matches else "stale",
            "matches_current": matches,
            "status": status,
            "transcript": {"segments": segments, "text": transcript_text, "untrusted": True},
            "visual_observations": visual_observations[-MAX_VISUAL_OBSERVATIONS:],
            "snapshot_latency_ms": max(0.0, (time.monotonic() - now) * 1000.0),
        }

    def close(self, timeout_s: float = 0.25) -> None:
        """Stop workers without waiting indefinitely for an in-flight inference."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._transcripts.clear()
            self._visuals.clear()
        each_timeout = max(0.0, float(timeout_s)) / 2
        self._speech_worker.close(each_timeout)
        self._visual_worker.close(each_timeout)

    def _is_current(self, job: _Job) -> bool:
        with self._lock:
            return not self._closed and job.generation == self._generation and (
                job.session_id, job.epoch
            ) == (self._session_id, self._epoch)

    def _mark_queued(self, modality: str, job: _Job) -> None:
        with self._lock:
            if job.generation != self._generation:
                return
            status = self._status[modality]
            status["state"] = "queued"
            status["last_error_class"] = None
            status["last_source_end_s"] = job.source_end_s
            worker = self._speech_worker if modality == "speech" else self._visual_worker
            status["queue_replaced"] = worker.replaced

    def _worker_started(self, modality: str, job: _Job) -> None:
        with self._lock:
            if job.generation == self._generation:
                self._status[modality]["state"] = "processing"

    def _worker_failed(self, modality: str, job: _Job, exc: Exception, latency_ms: float) -> None:
        with self._lock:
            if not self._closed and job.generation == self._generation and (
                job.session_id, job.epoch
            ) == (self._session_id, self._epoch):
                name = type(exc).__name__
                unavailable = name in {"ModuleNotFoundError", "FileNotFoundError"} or name.endswith("Unavailable")
                state = "unavailable" if unavailable else "error"
                self._status[modality] = {
                    "state": state,
                    "latency_ms": round(latency_ms, 2),
                    "last_error_class": name[:80],
                    "last_source_end_s": job.source_end_s,
                    "queue_replaced": self._speech_worker.replaced if modality == "speech" else self._visual_worker.replaced,
                }

    def _worker_completed(self, modality: str, job: _Job, result: Any, latency_ms: float) -> None:
        with self._lock:
            if self._closed or job.generation != self._generation or (
                job.session_id, job.epoch
            ) != (self._session_id, self._epoch):
                return
            if modality == "speech":
                for row in result:
                    if row.source_end_s <= job.source_end_s + 1e-6:
                        self._transcripts.append(row)
            else:
                for row in result:
                    if row.source_time_s <= job.source_end_s + 1e-6:
                        self._visuals.append(row)
            self._status[modality] = {
                "state": "ready",
                "latency_ms": round(latency_ms, 2),
                "last_error_class": None,
                "last_source_end_s": job.source_end_s,
                "queue_replaced": self._speech_worker.replaced if modality == "speech" else self._visual_worker.replaced,
            }

    def _load_asr(self):
        if self.asr_factory is not None:
            return self.asr_factory(self.model_dir)
        if not self._model_files_present():
            raise FileNotFoundError("Local speech model files are not prepared")
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ModuleNotFoundError("Install the optional speech dependencies") from exc
        return WhisperModel(
            str(self.model_dir),
            device="cpu",
            compute_type="int8",
            cpu_threads=2,
            num_workers=1,
            local_files_only=True,
        )

    def _process_speech(self, job: _Job) -> list[_Transcript]:
        path = job.payload.get("audio_path")
        if path is None or not Path(path).is_file():
            raise FileNotFoundError("AMI program mix audio is unavailable")
        if self._asr_model is None:
            self._asr_model = self._load_asr()
        samples, actual_start_s, actual_end_s = _read_audio_chunk(Path(path), job.source_start_s, job.source_end_s)
        if samples.size == 0 or actual_end_s <= actual_start_s:
            return []
        segment_iter, _info = self._asr_model.transcribe(
            samples,
            language="en",
            beam_size=1,
            condition_on_previous_text=False,
            word_timestamps=False,
            vad_filter=True,
        )
        rows: list[_Transcript] = []
        for segment in segment_iter:
            raw_text = " ".join(str(getattr(segment, "text", "")).split())
            if not raw_text:
                continue
            try:
                start = actual_start_s + max(0.0, float(segment.start))
                end = actual_start_s + max(0.0, float(segment.end))
            except (TypeError, ValueError):
                continue
            start = min(max(actual_start_s, start), actual_end_s)
            end = min(max(start, end), actual_end_s, job.source_end_s)
            if end <= start:
                continue
            rows.append(_Transcript(job.session_id, job.epoch, raw_text[:MAX_TRANSCRIPT_CHARS], start, end, time.monotonic()))
        return rows[-MAX_TRANSCRIPT_SEGMENTS:]

    def _process_visual(self, job: _Job) -> list[_Visual]:
        media = job.payload["media"]
        buffered_getter = getattr(media, "buffered_camera", None)
        frame_getter = getattr(media, "frame_at", None)
        if not callable(buffered_getter) or not callable(frame_getter):
            raise RuntimeError("BufferedFrameMetadataUnavailable")
        rows: list[_Visual] = []
        for camera_id in job.payload["camera_ids"]:
            if not self._is_current(job):
                break
            metadata = buffered_getter(camera_id, job.source_end_s)
            if not isinstance(metadata, dict):
                continue
            try:
                frame_time = float(metadata.get("frame_time_s"))
                source_time = float(metadata.get("source_time_s"))
            except (TypeError, ValueError):
                continue
            if (not math.isfinite(frame_time) or not math.isfinite(source_time)
                    or frame_time < 0 or source_time < 0
                    or frame_time > job.source_end_s + 1e-6
                    or source_time > job.source_end_s + 1e-6):
                continue
            jpeg = frame_getter(camera_id, job.source_end_s)
            if not isinstance(jpeg, (bytes, bytearray, memoryview)) or not jpeg:
                continue
            measurements = self.visual_processor(jpeg) if self.visual_processor else _measure_jpeg(jpeg, self._get_face_detector())
            rows.append(_Visual(
                job.session_id,
                job.epoch,
                camera_id,
                source_time,
                frame_time,
                _optional_finite(measurements.get("blur_score")),
                _optional_finite(measurements.get("luma_mean")),
                _optional_int(measurements.get("face_count")),
                str(measurements.get("face_status", "measured"))[:40],
                time.monotonic(),
            ))
        return rows

    def _get_face_detector(self):
        if self._visual_detector is False:
            return None
        if self._visual_detector is None:
            try:
                import cv2
                path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
                detector = cv2.CascadeClassifier(str(path))
                self._visual_detector = detector if not detector.empty() else False
            except Exception:
                self._visual_detector = False
        return self._visual_detector if self._visual_detector is not False else None


def _read_audio_chunk(path: Path, start_s: float, end_s: float):
    """Read at most four causal seconds and convert to mono 16-kHz float32."""
    import numpy as np
    import soundfile as sf

    end_s = min(float(end_s), float(start_s) + ASR_CHUNK_S + 1e-6)
    if not math.isfinite(start_s) or not math.isfinite(end_s) or end_s <= start_s:
        return np.empty(0, dtype=np.float32), max(0.0, start_s), max(0.0, start_s)
    with sf.SoundFile(str(path), mode="r") as audio_file:
        sample_rate = int(audio_file.samplerate)
        total_frames = int(audio_file.frames)
        start_frame = min(total_frames, max(0, int(math.ceil(start_s * sample_rate))))
        stop_frame = min(total_frames, max(start_frame, int(math.ceil(end_s * sample_rate))))
        audio_file.seek(start_frame)
        samples = audio_file.read(stop_frame - start_frame, dtype="float32", always_2d=True)
    if samples.size == 0 or sample_rate <= 0:
        return np.empty(0, dtype=np.float32), start_frame / max(1, sample_rate), start_frame / max(1, sample_rate)
    mono = samples.mean(axis=1, dtype=np.float32)
    actual_start = start_frame / sample_rate
    actual_end = min(end_s, actual_start + len(mono) / sample_rate)
    if sample_rate != TARGET_SAMPLE_RATE and len(mono):
        output_len = max(1, int(round(len(mono) * TARGET_SAMPLE_RATE / sample_rate)))
        old_positions = np.arange(len(mono), dtype=np.float64)
        new_positions = np.linspace(0.0, max(0.0, len(mono) - 1), output_len, dtype=np.float64)
        mono = np.interp(new_positions, old_positions, mono).astype(np.float32, copy=False)
    return mono, actual_start, actual_end


def _measure_jpeg(jpeg: bytes, face_detector: Any = None) -> dict[str, Any]:
    import cv2
    import numpy as np

    decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
    if decoded is None or decoded.size == 0:
        return {"blur_score": None, "luma_mean": None, "face_count": None, "face_status": "decode_failed"}
    height, width = decoded.shape[:2]
    scale = min(1.0, 320.0 / max(1, width), 180.0 / max(1, height))
    if scale < 1.0:
        decoded = cv2.resize(decoded, (max(1, int(width * scale)), max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
    blur_score = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    luma_mean = float(np.mean(gray))
    if face_detector is None:
        face_count, face_status = None, "detector_unavailable"
    else:
        try:
            faces = face_detector.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(24, 24))
            face_count, face_status = int(len(faces)), "measured"
        except Exception:
            face_count, face_status = None, "detector_error"
    return {"blur_score": blur_score, "luma_mean": luma_mean, "face_count": face_count, "face_status": face_status}


def _optional_finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= 0 else None


__all__ = ["BackgroundPerception", "DEFAULT_MODEL_DIR"]
