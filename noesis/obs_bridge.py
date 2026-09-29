"""Small, verified obs-websocket v5 bridge using simpleobsws."""

from __future__ import annotations

import asyncio
import os
from typing import Any


SCENE_NAME = "NOESIS_Program"
INPUT_NAME = "Noesis Program"
PROGRAM_URL = "http://127.0.0.1:8765/program?obs=1"


class OBSBridgeError(RuntimeError):
    pass


class SimpleOBSBridge:
    """Owns the OBS connection, the program browser source, and recording."""

    def __init__(
        self,
        *,
        url: str | None = None,
        password: str | None = None,
        client_factory: Any = None,
        timeout_s: float = 5.0,
    ) -> None:
        host = os.getenv("OBS_HOST", "127.0.0.1")
        port = os.getenv("OBS_PORT", "4455")
        self.url = url or os.getenv("OBS_WS_URL", f"ws://{host}:{port}")
        self._password = os.getenv("OBS_PASSWORD", "") if password is None else password
        self._client_factory = client_factory
        self.timeout_s = timeout_s
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._status = "disconnected"
        self._error: str | None = None
        self._recording = False
        self._output_path: str | None = None
        self._program_scene: str | None = None
        self._obs_version: str | None = None

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "connected": self._connected,
            "status": self._status,
            "recording": self._recording,
        }
        if self._error:
            result["error"] = self._safe_error(self._error)
        if self._output_path:
            result["output_path"] = self._output_path
        if self._program_scene:
            result["program_scene"] = self._program_scene
        if self._obs_version:
            result["version"] = self._obs_version
        return result

    async def poll_status(self) -> dict[str, Any]:
        """Read OBS state without involving the fast media-control loop."""
        async with self._lock:
            if not self._connected or self._client is None:
                return self.snapshot()
            try:
                await self._refresh_status()
            except Exception as exc:
                self._connected = False
                self._recording = False
                self._status = "disconnected"
                self._error = self._safe_error(str(exc) or type(exc).__name__)
                await self._disconnect_locked()
            return self.snapshot()

    async def connect(self) -> dict[str, Any]:
        async with self._lock:
            if self._connected and self._client is not None:
                try:
                    await self._request("GetVersion")
                    await self._refresh_status()
                    return self.snapshot()
                except Exception:
                    await self._disconnect_locked()
            try:
                factory = self._client_factory or self._default_client_factory()
                self._client = factory(url=self.url, password=self._password)
                connected = await asyncio.wait_for(self._client.connect(), timeout=self.timeout_s)
                if connected is False:
                    raise OBSBridgeError("OBS websocket connection was refused.")
                identified = await asyncio.wait_for(
                    self._client.wait_until_identified(timeout=int(self.timeout_s)),
                    timeout=self.timeout_s + 0.5,
                )
                if identified is False:
                    raise OBSBridgeError("OBS websocket identification timed out.")
                version = await self._request("GetVersion")
                self._obs_version = str(version.get("obsVersion", "unknown"))
                self._connected = True
                self._status = "connected"
                self._error = None
                await self._refresh_status()
                return self.snapshot()
            except Exception as exc:
                await self._disconnect_locked()
                self._connected = False
                self._status = "error"
                self._error = self._safe_error(str(exc) or type(exc).__name__)
                raise OBSBridgeError(self._error) from exc

    async def disconnect(self) -> None:
        async with self._lock:
            await self._disconnect_locked()
            self._connected = False
            self._status = "disconnected"
            self._recording = False

    async def setup_program_scene(self) -> dict[str, Any]:
        async with self._lock:
            self._require_connected()
            try:
                scenes = (await self._request("GetSceneList")).get("scenes", [])
                scene_exists = any(item.get("sceneName") == SCENE_NAME for item in scenes if isinstance(item, dict))
                if not scene_exists:
                    await self._request("CreateScene", {"sceneName": SCENE_NAME})

                scene_items = (await self._request("GetSceneItemList", {"sceneName": SCENE_NAME})).get("sceneItems", [])
                has_item = any(
                    isinstance(item, dict) and item.get("sourceName") == INPUT_NAME
                    for item in scene_items
                )
                input_list = (await self._request("GetInputList")).get("inputs", [])
                input_exists = any(
                    isinstance(item, dict) and item.get("inputName") == INPUT_NAME
                    for item in input_list
                )
                settings = {
                    "is_local_file": False,
                    "url": PROGRAM_URL,
                    "width": 1280,
                    "height": 720,
                    "fps_custom": True,
                    "fps": 30,
                    "shutdown": False,
                    "restart_when_active": False,
                    "reroute_audio": True,
                }
                if input_exists:
                    await self._request("SetInputSettings", {
                        "inputName": INPUT_NAME,
                        "inputSettings": settings,
                        "overlay": True,
                    })
                    if not has_item:
                        await self._request("CreateSceneItem", {
                            "sceneName": SCENE_NAME,
                            "sourceName": INPUT_NAME,
                        })
                else:
                    await self._request("CreateInput", {
                        "sceneName": SCENE_NAME,
                        "inputName": INPUT_NAME,
                        "inputKind": "browser_source",
                        "inputSettings": settings,
                        "sceneItemEnabled": True,
                    })
                refreshed = (await self._request("GetSceneItemList", {"sceneName": SCENE_NAME})).get("sceneItems", [])
                program_item = next(
                    (item for item in refreshed if isinstance(item, dict) and item.get("sourceName") == INPUT_NAME),
                    None,
                )
                if program_item is None:
                    raise OBSBridgeError("OBS did not confirm the Noesis browser source in NOESIS_Program.")
                if program_item.get("sceneItemEnabled") is False:
                    await self._request("SetSceneItemEnabled", {
                        "sceneName": SCENE_NAME,
                        "sceneItemId": program_item["sceneItemId"],
                        "sceneItemEnabled": True,
                    })
                self._program_scene = SCENE_NAME
                self._status = "ready"
                self._error = None
                await self._refresh_status()
                return {
                    "scene_name": SCENE_NAME,
                    "browser_input": INPUT_NAME,
                    "url": PROGRAM_URL,
                    "scene_created": not scene_exists,
                    "source_created": not input_exists,
                    "browser_source_confirmed": True,
                }
            except Exception as exc:
                self._status = "error"
                self._error = self._safe_error(str(exc) or type(exc).__name__)
                raise OBSBridgeError(self._error) from exc

    async def select_program_scene(self, scene_name: str = SCENE_NAME) -> dict[str, Any]:
        if scene_name != SCENE_NAME:
            raise OBSBridgeError("Only NOESIS_Program is allowed for Noesis output.")
        async with self._lock:
            self._require_connected()
            try:
                await self._request("SetCurrentProgramScene", {"sceneName": scene_name})
                current = await self._request("GetCurrentProgramScene")
                actual = current.get("currentProgramSceneName")
                if actual != scene_name:
                    raise OBSBridgeError(f"OBS scene readback was {actual!r}, not {scene_name!r}.")
                self._program_scene = actual
                self._status = "ready"
                self._error = None
                return {"scene_name": actual, "confirmed": True}
            except Exception as exc:
                self._status = "error"
                self._error = self._safe_error(str(exc) or type(exc).__name__)
                raise OBSBridgeError(self._error) from exc

    async def start_recording(self) -> dict[str, Any]:
        async with self._lock:
            self._require_connected()
            try:
                before = await self._request("GetRecordStatus")
                if not before.get("outputActive"):
                    await self._request("StartRecord")
                status = await self._wait_for_recording(True)
                self._recording = True
                path = status.get("outputPath")
                self._output_path = str(path) if path else self._output_path
                self._status = "recording"
                self._error = None
                return {"recording": True, **({"output_path": self._output_path} if self._output_path else {})}
            except Exception as exc:
                self._status = "error"
                self._recording = False
                self._error = self._safe_error(str(exc) or type(exc).__name__)
                raise OBSBridgeError(self._error) from exc

    async def stop_recording(self) -> dict[str, Any]:
        async with self._lock:
            if not self._connected or self._client is None:
                raise OBSBridgeError("Recording stop is unconfirmed while OBS is disconnected. Reconnect and retry Stop.")
            try:
                status = await self._request("GetRecordStatus")
                output_path = status.get("outputPath")
                if status.get("outputActive"):
                    stopped = await self._request("StopRecord")
                    output_path = stopped.get("outputPath") or output_path
                    confirm = await self._wait_for_recording(False)
                else:
                    confirm = status
                if confirm.get("outputActive"):
                    raise OBSBridgeError("OBS still reports an active recording after StopRecord.")
                self._recording = False
                self._output_path = str(output_path) if output_path else self._output_path
                self._status = "ready"
                self._error = None
                result = {"recording": False, "stopped": True}
                if self._output_path:
                    result["output_path"] = self._output_path
                return result
            except Exception as exc:
                self._status = "error"
                self._error = self._safe_error(str(exc) or type(exc).__name__)
                raise OBSBridgeError(self._error) from exc

    async def _wait_for_recording(self, expected: bool) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + self.timeout_s
        last_status: dict[str, Any] = {}
        while True:
            last_status = await self._request("GetRecordStatus")
            if bool(last_status.get("outputActive", False)) is expected:
                return last_status
            if asyncio.get_running_loop().time() >= deadline:
                target = "active" if expected else "stopped"
                raise OBSBridgeError(f"OBS did not confirm recording {target} within {self.timeout_s:g}s.")
            await asyncio.sleep(0.15)

    async def _refresh_status(self) -> None:
        if not self._connected or self._client is None:
            return
        try:
            program = await self._request("GetCurrentProgramScene")
            record = await self._request("GetRecordStatus")
            self._program_scene = program.get("currentProgramSceneName")
            self._recording = bool(record.get("outputActive", False))
            if record.get("outputPath"):
                self._output_path = str(record["outputPath"])
            if self._recording:
                self._status = "recording"
            elif self._status not in ("ready", "error"):
                self._status = "connected"
            self._error = None
        except Exception as exc:
            self._error = self._safe_error(str(exc) or type(exc).__name__)
            self._status = "error"
            raise

    async def _request(self, request_type: str, request_data: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._client is None:
            raise OBSBridgeError("OBS is not connected.")
        try:
            import simpleobsws

            response = await asyncio.wait_for(
                self._client.call(simpleobsws.Request(request_type, request_data), timeout=int(self.timeout_s)),
                timeout=self.timeout_s + 0.5,
            )
        except Exception as exc:
            raise OBSBridgeError(f"OBS request {request_type} failed: {type(exc).__name__}: {exc}") from exc
        if not response.ok():
            status = getattr(response, "requestStatus", None)
            code = getattr(status, "code", "unknown")
            comment = getattr(status, "comment", "request rejected")
            raise OBSBridgeError(f"OBS request {request_type} rejected ({code}): {comment}")
        payload = response.responseData or {}
        if not isinstance(payload, dict):
            raise OBSBridgeError(f"OBS request {request_type} returned invalid response data.")
        return payload

    def _require_connected(self) -> None:
        if not self._connected or self._client is None:
            raise OBSBridgeError("OBS is not connected. Call /api/obs/connect first.")

    async def _disconnect_locked(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), timeout=self.timeout_s)
            except Exception:
                pass

    def _default_client_factory(self) -> Any:
        try:
            import simpleobsws
        except ImportError as exc:
            raise OBSBridgeError("simpleobsws is not installed.") from exc
        return simpleobsws.WebSocketClient

    def _safe_error(self, message: str) -> str:
        safe = str(message)
        if self._password:
            safe = safe.replace(self._password, "[redacted]")
        return safe[:500]
