from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import local_agents
from noesis import model_gateway
from noesis.controller import DirectorController
from test_controller import FakeMedia, FakeOBS


def test_local_launcher_routes_credentials_only_to_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(local_agents, "RUNTIME", tmp_path)
    monkeypatch.setattr(model_gateway, "dotenv_values", lambda _path: {})
    source = {
        "NEBIUS_KIMI_API_ENDPOINT": "https://api.tokenfactory.tf-ca1.nebius.com/v1/responses",
        "NEBIUS_KIMI_MODEL": "test-kimi", "NEBIUS_KIMI_API_KEY": "private-provider-key",
        "NEBIUS_MINIMAX_API_ENDPOINT": "https://api.tokenfactory.tf-ca1.nebius.com/v1/responses",
        "NEBIUS_MINIMAX_MODEL": "optional-minimax", "NEBIUS_MINIMAX_API_KEY": "",
        "FLWR_RUNTIME_API_KEY": "stale-parent-token", "NOESIS_GATEWAY_TOKEN": "stale-gateway-token",
        "NOESIS_RUNTIME_DEPLOYMENT": "supergrid", "NOESIS_GRID_RUN_FILE": "old-cloud-run.json",
    }
    app, gateway, link = local_agents.environments(source, "kimi")
    assert "private-provider-key" not in json.dumps(app)
    assert "private-provider-key" not in json.dumps(link)
    assert gateway["NEBIUS_KIMI_API_KEY"] == "private-provider-key"
    assert link["FLWR_MODEL_API_KEY"] == gateway["NOESIS_GATEWAY_TOKEN"]
    assert link["FLWR_MODEL_API_ENDPOINT"] == "http://127.0.0.1:8770/v1/responses"
    assert "FLWR_RUNTIME_API_KEY" not in link and "NOESIS_GATEWAY_TOKEN" not in app
    assert app["NOESIS_RUNTIME_DEPLOYMENT"] == "local"
    assert app["NOESIS_GRID_RUN_FILE"] == str(local_agents.RUN_FILE)
    assert [p["id"] for p in json.loads(app["NOESIS_MODEL_CATALOG_JSON"])] == ["kimi"]


@pytest.mark.asyncio
@pytest.mark.parametrize("deployment", ["local", "supergrid"])
async def test_controller_reports_actual_deployment(monkeypatch, tmp_path, deployment):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"run_id": "123", "deployment": deployment, "status": "running", "secret": "not-for-ui"}))
    monkeypatch.setenv("NOESIS_GRID_RUN_FILE", str(path))
    monkeypatch.setenv("NOESIS_RUNTIME_DEPLOYMENT", deployment)
    media = FakeMedia()
    controller = DirectorController(media, FakeOBS(media))
    state = await controller.get_state()
    assert state["flower"]["deployment"] == deployment
    assert state["flower"]["grid_run"]["deployment"] == deployment
    assert "not-for-ui" not in json.dumps(state)
