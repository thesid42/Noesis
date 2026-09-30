"""FastAPI application and the local HTTP broker for Noesis."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from .controller import ControllerError, DirectorController
from .ai_control import AILeaseFailureBody, AIResultBody
from .obs_bridge import SimpleOBSBridge


PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"


class StartBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_mode: Literal["synthetic", "ami"] = "synthetic"
    output_mode: Literal["preview", "obs"] = "preview"
    start_s: float = Field(default=0.0, ge=0.0)
    output_delay_s: float | None = Field(default=None, ge=0.0, le=15.0)


class EmptyBody(BaseModel):
    model_config = ConfigDict(extra="ignore")


class SeekBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time_s: float = Field(ge=0.0)


class OverrideBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(min_length=1, max_length=64)


class FaultBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(min_length=1, max_length=64)
    kind: Literal["black", "freeze", "offline", "none"]
    duration_s: float = Field(default=10.0, gt=0.0, le=300.0)


class HeartbeatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_id: str = Field(min_length=1, max_length=120)
    role: Literal["camera", "director", "critic"]
    runtime: str = Field(min_length=1, max_length=120)
    run_id: str | None = Field(default=None, max_length=200)
    camera_id: str | None = Field(default=None, max_length=64)
    decision_mode: Literal["rules", "llm"] | None = None


class ObservationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    camera_id: str = Field(min_length=1, max_length=64)
    source_observation_revision: int = Field(ge=0)
    observation: dict[str, Any]


class ModelSelectBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Literal["kimi", "minimax"]


class MissingMedia:
    """Visible service placeholder used only before the media module is present."""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir

    def start(self, input_mode: str = "synthetic", start_s: float = 0.0) -> None:
        raise RuntimeError("noesis.media.MediaEngine is unavailable")

    def pause(self) -> None:
        raise RuntimeError("MediaEngine is unavailable")

    def resume(self) -> None:
        raise RuntimeError("MediaEngine is unavailable")

    def stop(self) -> None:
        return None

    def seek(self, time_s: float) -> None:
        raise RuntimeError("MediaEngine is unavailable")

    def inject_fault(self, camera_id: str, kind: str, duration_s: float = 10.0) -> None:
        raise RuntimeError("MediaEngine is unavailable")

    def snapshot(self) -> dict[str, Any]:
        return {
            "time_s": 0.0,
            "duration_s": 0.0,
            "input_mode": "synthetic",
            "status": "unavailable",
            "observation_revision": 0,
            "cameras": [
                {
                    "id": camera_id,
                    "name": camera_id,
                    "participant": {"closeup1": "A", "closeup2": "C", "closeup3": "D", "closeup4": "B"}.get(camera_id),
                    "healthy": False,
                    "status": "unavailable",
                    "speaking": False,
                    "energy": 0.0,
                    "quality": 0.0,
                    "age_ms": 0,
                    "frame_url": f"/api/frame/{camera_id}.jpg",
                }
                for camera_id in ("closeup1", "closeup2", "closeup3", "closeup4", "corner")
            ],
        }

    def frame_jpeg(self, camera_id: str) -> bytes:
        raise RuntimeError("MediaEngine is unavailable")

    def audio_path(self) -> Path | None:
        return None

    def availability(self) -> dict[str, Any]:
        return {"ami_available": False, "missing_files": ["MediaEngine unavailable"], "credits": ""}


def _default_media() -> Any:
    data_dir_value = os.getenv("NOESIS_DATA_DIR", "data/ami/ES2002a")
    data_dir = Path(data_dir_value)
    try:
        from .media import MediaEngine  # owned by the media executor

        return MediaEngine(data_dir=data_dir, output_delay_s=float(os.getenv("NOESIS_OUTPUT_DELAY_S", "5")))
    except ImportError:
        return MissingMedia(data_dir)


def create_app(
    *,
    media: Any | None = None,
    obs: Any | None = None,
    controller: DirectorController | None = None,
    auto_connect_obs: bool | None = None,
) -> FastAPI:
    media_engine = media if media is not None else _default_media()
    obs_bridge = obs if obs is not None else SimpleOBSBridge()
    perception = None
    if controller is None and hasattr(media_engine, "frame_at"):
        from .perception import BackgroundPerception
        perception = BackgroundPerception(media_engine, Path(__file__).resolve().parents[1] / ".runtime/models/whisper-tiny.en")
    director = controller or DirectorController(
        media_engine,
        obs_bridge,
        model_name=os.getenv("NOESIS_MODEL") or None,
        model_catalog=json.loads(os.getenv("NOESIS_MODEL_CATALOG_JSON", "null")),
        model_profile=os.getenv("NOESIS_MODEL_PROFILE") or "kimi",
        perception=perception,
    )
    should_auto_connect = (
        os.getenv("OBS_AUTOCONNECT", "false").strip().lower() in {"1", "true", "yes"}
        if auto_connect_obs is None
        else auto_connect_obs
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.controller = director
        await director.start_background()
        if should_auto_connect:
            try:
                await director.connect_obs()
            except ControllerError:
                pass
        try:
            yield
        finally:
            await director.close()

    app = FastAPI(title="Noesis", version="0.1.0", lifespan=lifespan)
    app.state.controller = director

    @app.exception_handler(ControllerError)
    async def handle_controller_error(_request: Request, exc: ControllerError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        return await director.get_state()

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        async def event_stream():
            while not await request.is_disconnected():
                state = await director.get_state()
                # Send complete snapshots at a bounded cadence so replay time
                # and source health stay fresh even when no editorial event fires.
                encoded = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
                yield f"event: state\ndata: {encoded}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/session/start")
    async def session_start(body: StartBody) -> dict[str, Any]:
        return await director.session_start(body.input_mode, body.output_mode, body.start_s, body.output_delay_s)

    @app.post("/api/session/pause")
    async def session_pause(_body: EmptyBody) -> dict[str, Any]:
        return await director.session_pause()

    @app.post("/api/session/resume")
    async def session_resume(_body: EmptyBody) -> dict[str, Any]:
        return await director.session_resume()

    @app.post("/api/session/stop")
    async def session_stop(_body: EmptyBody) -> dict[str, Any]:
        return await director.session_stop()

    @app.post("/api/session/seek")
    async def session_seek(body: SeekBody) -> dict[str, Any]:
        return await director.session_seek(body.time_s)

    @app.post("/api/control/override")
    async def control_override(body: OverrideBody) -> dict[str, Any]:
        return await director.manual_override(body.camera_id)

    @app.post("/api/control/autopilot")
    async def control_autopilot(_body: EmptyBody) -> dict[str, Any]:
        return await director.resume_autopilot()

    @app.post("/api/fault")
    async def inject_fault(body: FaultBody) -> dict[str, Any]:
        return await director.inject_fault(body.camera_id, body.kind, body.duration_s)

    @app.get("/api/frame/{camera_id}.jpg")
    async def camera_frame(camera_id: str) -> Response:
        payload = await director.get_frame(camera_id)
        return Response(payload, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/program.jpg")
    async def program_frame() -> Response:
        payload, _camera_id = await director.get_program_frame()
        return Response(payload, media_type="image/jpeg", headers={"Cache-Control": "no-store, max-age=0"})

    @app.get("/api/program.mjpeg")
    async def program_stream(request: Request) -> StreamingResponse:
        async def frames():
            while not await request.is_disconnected():
                started = asyncio.get_running_loop().time()
                payload, _ = await director.get_program_frame()
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(payload)).encode() + b"\r\n\r\n" + payload + b"\r\n"
                await asyncio.sleep(max(0.001, 1 / 30 - (asyncio.get_running_loop().time() - started)))
        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/ai/lease/{agent_id}")
    async def ai_lease(agent_id: str):
        return await director.ai_lease(agent_id)

    @app.post("/api/ai/lease/failure")
    async def ai_lease_failure(body: AILeaseFailureBody) -> dict[str, Any]:
        return await director.fail_ai_lease(body.model_dump(exclude_none=True))

    @app.get("/api/audio")
    async def program_audio() -> Response:
        audio_path = await director.get_audio_path()
        if not audio_path or not Path(audio_path).is_file():
            return JSONResponse(status_code=404, content={"detail": "Continuous AMI program audio is unavailable."})
        return FileResponse(
            audio_path,
            media_type="audio/wav",
            filename=Path(audio_path).name,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/obs/connect")
    async def obs_connect(_body: EmptyBody) -> dict[str, Any]:
        return await director.connect_obs()

    @app.post("/api/obs/setup")
    async def obs_setup(_body: EmptyBody) -> dict[str, Any]:
        return await director.setup_obs()

    @app.get("/api/agents/snapshot")
    async def agents_snapshot() -> dict[str, Any]:
        return await director.agents_snapshot()

    @app.post("/api/agents/heartbeat")
    async def agent_heartbeat(body: HeartbeatBody) -> dict[str, Any]:
        return await director.heartbeat(body.model_dump(exclude_none=True))

    @app.post("/api/agents/observations")
    async def agent_observation(body: ObservationBody) -> dict[str, Any]:
        return await director.camera_observation(body.model_dump())

    @app.post("/api/models/select")
    async def select_model(body: ModelSelectBody) -> dict[str, Any]:
        return await director.select_model(body.profile)

    @app.get("/api/ai/round")
    async def ai_round() -> dict[str, Any] | None:
        return await director.ai_round()

    @app.post("/api/ai/report")
    async def ai_report(body: AIResultBody) -> dict[str, Any]:
        return await director.accept_ai_report(body.model_dump(exclude_none=True))

    @app.post("/api/ai/decision")
    async def ai_decision(body: AIResultBody) -> dict[str, Any]:
        return await director.accept_ai_decision(body.model_dump(exclude_none=True))

    @app.get("/program", response_class=HTMLResponse)
    async def program_page() -> Response:
        # The frontend owner supplies this file. Looking it up per request keeps
        # the route useful when static assets arrive after app construction.
        page = STATIC_DIR / "program.html"
        if page.is_file():
            return FileResponse(page, media_type="text/html")
        return HTMLResponse(
            "<!doctype html><html><body style='background:#111;color:#eee;font:16px sans-serif'>"
            "<h1>Noesis program source is not ready</h1>"
            "<p>The frontend program page has not been installed.</p></body></html>",
            status_code=503,
        )

    @app.get("/")
    async def dashboard() -> Response:
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return FileResponse(page, media_type="text/html")
        return JSONResponse({"service": "Noesis", "status": "starting", "api": "/api/state"})

    if STATIC_DIR.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")
    return app


app = create_app()
