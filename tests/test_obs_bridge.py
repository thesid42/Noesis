from __future__ import annotations

from types import SimpleNamespace

import pytest

from noesis.obs_bridge import INPUT_NAME, PROGRAM_URL, SCENE_NAME, SimpleOBSBridge, OBSBridgeError


class FakeOBSClient:
    def __init__(self, **_kwargs):
        self.connected = False
        self.scenes = [{"sceneName": "Unrelated Scene"}]
        self.inputs = []
        self.items = {"Unrelated Scene": []}
        self.current_scene = "Unrelated Scene"
        self.recording = False
        self.output_path = "C:/recordings/noesis.mkv"
        self.calls = []
        self.failure: str | None = None

    async def connect(self):
        self.connected = True
        return True

    async def wait_until_identified(self, timeout=10):
        return True

    async def disconnect(self):
        self.connected = False
        return True

    async def call(self, request, timeout=15):
        if self.failure:
            raise ConnectionError(self.failure)
        name = request.requestType
        data = request.requestData or {}
        self.calls.append((name, data))
        response = {}
        if name == "GetVersion":
            response = {"obsVersion": "32.0.0"}
        elif name == "GetCurrentProgramScene":
            response = {"currentProgramSceneName": self.current_scene}
        elif name == "GetRecordStatus":
            response = {"outputActive": self.recording, "outputPath": self.output_path if self.recording else ""}
        elif name == "GetSceneList":
            response = {"scenes": list(self.scenes)}
        elif name == "CreateScene":
            self.scenes.append({"sceneName": data["sceneName"]})
            self.items[data["sceneName"]] = []
        elif name == "GetSceneItemList":
            response = {"sceneItems": list(self.items.get(data["sceneName"], []))}
        elif name == "GetInputList":
            response = {"inputs": list(self.inputs)}
        elif name == "CreateInput":
            self.inputs.append({"inputName": data["inputName"], "inputKind": data["inputKind"]})
            self.items[data["sceneName"]].append({
                "sourceName": data["inputName"],
                "sceneItemId": 42,
                "sceneItemEnabled": data.get("sceneItemEnabled", True),
            })
        elif name == "SetInputSettings":
            pass
        elif name == "PressInputPropertiesButton":
            response = {}
        elif name == "CreateSceneItem":
            self.items[data["sceneName"]].append({
                "sourceName": data["sourceName"],
                "sceneItemId": 42,
                "sceneItemEnabled": True,
            })
        elif name == "SetSceneItemEnabled":
            for item in self.items[data["sceneName"]]:
                if item["sceneItemId"] == data["sceneItemId"]:
                    item["sceneItemEnabled"] = data["sceneItemEnabled"]
        elif name == "SetCurrentProgramScene":
            self.current_scene = data["sceneName"]
        elif name == "StartRecord":
            self.recording = True
        elif name == "StopRecord":
            self.recording = False
            response = {"outputPath": self.output_path}
        return SimpleNamespace(
            ok=lambda: True,
            responseData=response,
            requestStatus=SimpleNamespace(code=100, comment=""),
        )


@pytest.mark.asyncio
async def test_obs_setup_preserves_scenes_and_confirms_program_and_recording():
    client = FakeOBSClient()
    bridge = SimpleOBSBridge(
        url="ws://127.0.0.1:4455",
        password="test-secret",
        client_factory=lambda **kwargs: client,
    )

    connected = await bridge.connect()
    assert connected["connected"] is True
    setup = await bridge.setup_program_scene()
    assert setup["scene_name"] == SCENE_NAME
    assert setup["browser_source_confirmed"] is True
    assert [scene["sceneName"] for scene in client.scenes] == ["Unrelated Scene", SCENE_NAME]
    create = next(data for name, data in client.calls if name == "CreateInput")
    assert create["inputName"] == INPUT_NAME
    assert create["inputKind"] == "browser_source"
    assert create["inputSettings"]["url"] == PROGRAM_URL
    assert create["inputSettings"]["width"] == 1280
    assert create["inputSettings"]["height"] == 720
    assert create["inputSettings"]["shutdown"] is False
    assert create["inputSettings"]["restart_when_active"] is False
    assert create["inputSettings"]["reroute_audio"] is True

    selected = await bridge.select_program_scene()
    assert selected == {"scene_name": SCENE_NAME, "confirmed": True}
    started = await bridge.start_recording()
    assert started["recording"] is True
    assert bridge.snapshot()["recording"] is True
    stopped = await bridge.stop_recording()
    assert stopped["recording"] is False
    assert stopped["output_path"] == "C:/recordings/noesis.mkv"
    assert bridge.snapshot()["recording"] is False
    assert all("test-secret" not in repr(call) for call in client.calls)


@pytest.mark.asyncio
async def test_status_poll_reflects_external_stop_and_connection_loss_without_secrets():
    client = FakeOBSClient()
    bridge = SimpleOBSBridge(
        url="ws://127.0.0.1:4455",
        password="test-secret",
        client_factory=lambda **kwargs: client,
    )
    await bridge.connect()
    await bridge.setup_program_scene()
    await bridge.start_recording()

    client.recording = False  # An operator stopped recording directly in OBS.
    observed = await bridge.poll_status()
    assert observed["connected"] is True
    assert observed["recording"] is False
    assert observed["status"] != "recording"

    client.failure = "websocket closed after test-secret was rotated"
    observed = await bridge.poll_status()
    assert observed["connected"] is False
    assert observed["recording"] is False
    assert observed["status"] == "disconnected"
    assert "test-secret" not in observed["error"]


@pytest.mark.asyncio
async def test_setup_refreshes_only_existing_noesis_browser_source():
    client = FakeOBSClient()
    client.scenes.append({"sceneName": SCENE_NAME})
    client.items[SCENE_NAME] = [{
        "sourceName": INPUT_NAME,
        "sceneItemId": 7,
        "sceneItemEnabled": True,
    }]
    client.inputs.append({"inputName": INPUT_NAME, "inputKind": "browser_source"})
    client.inputs.append({"inputName": "Unrelated Browser", "inputKind": "browser_source"})
    bridge = SimpleOBSBridge(client_factory=lambda **kwargs: client)

    await bridge.connect()
    await bridge.setup_program_scene()

    refreshes = [data for name, data in client.calls if name == "PressInputPropertiesButton"]
    assert refreshes == [{"inputName": INPUT_NAME, "propertyName": "refreshnocache"}]
    assert client.inputs == [
        {"inputName": INPUT_NAME, "inputKind": "browser_source"},
        {"inputName": "Unrelated Browser", "inputKind": "browser_source"},
    ]


@pytest.mark.asyncio
async def test_setup_refuses_to_refresh_browser_source_while_recording():
    client = FakeOBSClient()
    client.recording = True
    bridge = SimpleOBSBridge(client_factory=lambda **kwargs: client)
    await bridge.connect()
    client.calls.clear()

    with pytest.raises(OBSBridgeError, match="while OBS is recording"):
        await bridge.setup_program_scene()

    assert [name for name, _data in client.calls] == ["GetRecordStatus"]
    assert not any(name in {"CreateScene", "CreateInput", "SetInputSettings", "PressInputPropertiesButton"}
                   for name, _data in client.calls)


@pytest.mark.asyncio
async def test_disconnected_stop_does_not_claim_success_and_reconnect_can_stop():
    client = FakeOBSClient()
    bridge = SimpleOBSBridge(client_factory=lambda **kwargs: client)
    await bridge.connect()
    await bridge.start_recording()
    client.failure = "temporary socket outage"
    await bridge.poll_status()
    with pytest.raises(OBSBridgeError, match="unconfirmed"):
        await bridge.stop_recording()
    assert client.recording
    client.failure = None
    await bridge.connect()
    result = await bridge.stop_recording()
    assert result["stopped"] and not client.recording
