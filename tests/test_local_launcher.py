from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import local_agents
import run_demo
from noesis import model_gateway
from noesis.controller import DirectorController
from test_controller import FakeMedia, FakeOBS


@pytest.mark.parametrize("inference_transport", [None, "flower", "gateway"])
def test_local_launcher_routes_credentials_only_to_gateway(monkeypatch, tmp_path, inference_transport):
    monkeypatch.setattr(local_agents, "RUNTIME", tmp_path)
    monkeypatch.setattr(model_gateway, "dotenv_values", lambda _path: {})
    source = {
        "NEBIUS_KIMI_API_ENDPOINT": "https://api.tokenfactory.tf-ca1.nebius.com/v1/responses",
        "NEBIUS_KIMI_MODEL": "test-kimi", "NEBIUS_KIMI_API_KEY": "private-provider-key",
        "NEBIUS_MINIMAX_API_ENDPOINT": "https://api.tokenfactory.tf-ca1.nebius.com/v1/responses",
        "NEBIUS_MINIMAX_MODEL": "optional-minimax", "NEBIUS_MINIMAX_API_KEY": "",
        "FLWR_RUNTIME_API_KEY": "stale-parent-token", "NOESIS_GATEWAY_TOKEN": "stale-gateway-token",
        "NOESIS_AGENT_GATEWAY_TOKEN": "stale-agent-token", "NOESIS_INFERENCE_TRANSPORT": "invalid",
        "NOESIS_RUNTIME_DEPLOYMENT": "supergrid", "NOESIS_GRID_RUN_FILE": "old-cloud-run.json",
    }
    if inference_transport is None:
        app, gateway, link = local_agents.environments(source, "kimi")
        inference_transport = "gateway"
    else:
        app, gateway, link = local_agents.environments(source, "kimi", inference_transport)
    assert "private-provider-key" not in json.dumps(app)
    assert "private-provider-key" not in json.dumps(link)
    assert gateway["NEBIUS_KIMI_API_KEY"] == "private-provider-key"
    assert link["FLWR_MODEL_API_KEY"] == gateway["NOESIS_GATEWAY_TOKEN"]
    assert link["FLWR_MODEL_API_ENDPOINT"] == "http://127.0.0.1:8770/v1/responses"
    assert "FLWR_RUNTIME_API_KEY" not in link and "NOESIS_GATEWAY_TOKEN" not in app
    assert "NOESIS_AGENT_GATEWAY_TOKEN" not in app and "NOESIS_AGENT_GATEWAY_TOKEN" not in gateway
    assert app["NOESIS_INFERENCE_TRANSPORT"] == inference_transport
    if inference_transport == "gateway":
        assert link["NOESIS_AGENT_GATEWAY_TOKEN"] == gateway["NOESIS_GATEWAY_TOKEN"]
    else:
        assert "NOESIS_AGENT_GATEWAY_TOKEN" not in link
    assert app["NOESIS_RUNTIME_DEPLOYMENT"] == "local"
    assert app["NOESIS_GRID_RUN_FILE"] == str(local_agents.RUN_FILE)
    assert [p["id"] for p in json.loads(app["NOESIS_MODEL_CATALOG_JSON"])] == ["kimi"]


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["local", "supergrid"])
@pytest.mark.parametrize("inference_transport", ["flower", "gateway"])
async def test_controller_reports_actual_deployment(monkeypatch, tmp_path, deployment, inference_transport):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"run_id": "123", "deployment": deployment, "status": "running", "secret": "not-for-ui"}))
    monkeypatch.setenv("NOESIS_GRID_RUN_FILE", str(path))
    monkeypatch.setenv("NOESIS_RUNTIME_DEPLOYMENT", deployment)
    monkeypatch.setenv("NOESIS_INFERENCE_TRANSPORT", inference_transport)
    media = FakeMedia()
    controller = DirectorController(media, FakeOBS(media))
    state = await controller.get_state()
    assert state["flower"]["deployment"] == deployment
    assert state["flower"]["grid_run"]["deployment"] == deployment
    expected = inference_transport if deployment == "local" else "flower"
    assert state["flower"]["inference_transport"] == expected
    if expected == "gateway":
        assert "persistent gateway" in state["flower"]["transport"]
    assert "not-for-ui" not in json.dumps(state)


@pytest.mark.parametrize("argv,expected", [
    ([], "gateway"),
    (["--runtime", "supergrid"], "flower"),
    (["--inference-transport", "flower"], "flower"),
])
def test_demo_transport_default_matches_runtime(argv, expected):
    assert run_demo.parse_args(argv).inference_transport == expected


def test_demo_rejects_direct_gateway_on_supergrid():
    with pytest.raises(SystemExit) as error:
        run_demo.parse_args(["--runtime", "supergrid", "--inference-transport", "gateway"])
    assert error.value.code == 2
