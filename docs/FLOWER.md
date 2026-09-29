# Noesis Flower AgentApps

Noesis runs five independent Flower AgentApps through the local Flower SuperLink: one Camera AgentApp for each participant close-up and one Director AgentApp. Each process runs under a real Flower run ID. The camera agents read the current source snapshot, report compact timestamped observations to the local Noesis HTTP broker, and heartbeat separately. The Director reads expiring requests, checks recent same-session camera-agent evidence, and sends a proposal with the controller-issued request ID, session epoch, override epoch, and observation revision.

The runs use application-managed HTTP messaging between Flower AgentApps and the local controller. Raw frames and audio stay on the local media path. The Corner source is covered by the deterministic controller and does not need a participant Camera AgentApp.

## Start and inspect

Start the Noesis server and make sure OBS is available if the session will use the OBS output. Then start the agents from the project environment:

```powershell
.\.venv\Scripts\python.exe scripts\flower_agents.py start
.\.venv\Scripts\python.exe scripts\flower_agents.py status
```

The launcher reuses a reachable SuperLink at `127.0.0.1:8000`. If none is running, it starts one with the workspace Flower environment. It checks that the Noesis broker at `127.0.0.1:8765` is responding before submitting runs.

The default run is bounded to one hour per AgentApp and uses the rules Director. It does not need model credentials. Flower assigns a distinct run ID to each process; the launcher records only those run IDs in `.runtime/flower-agents.json`.

Stop the runs owned by this launcher with:

```powershell
.\.venv\Scripts\python.exe scripts\flower_agents.py stop
```

Stopping these runs leaves the shared SuperLink running. If a run stops unexpectedly, inspect its actual Flower status and logs with `status` and the Flower Control API or CLI before restarting.

The lifecycle script uses the Flower 1.39 `ControlHttpClient` and builds a local FAB from `flower_apps/`. It passes a JSON run prompt in `StartRunRequest.user_prompt`, which is required by the supported runtime path used here.

## Optional model-backed Director

The deterministic Director is the default and remains the fallback. To request an optional model decision, provide an upstream Open Responses-compatible provider to the SuperLink process using Flower's documented environment variables, then start a new set of runs with an available Flower runtime model name:

```powershell
.\.venv\Scripts\python.exe scripts\flower_agents.py stop
.\.venv\Scripts\python.exe scripts\flower_agents.py start --director-mode llm --model '<available-model-name>'
```

The AgentApp reads `FLWR_RUNTIME_BASE_URL` and `FLWR_RUNTIME_API_KEY` only inside its Flower run and sends the request through Flower's injected Responses endpoint. The model sees the current request, the rules baseline, and compact reports from camera agents; it does not receive media or future annotations. Its JSON action is checked again against the current request and fresh camera reports. The call is bounded by the request deadline. A timeout, missing runtime access, invalid JSON, or disallowed target falls through to the rules Director. Heartbeats use a separate thread while a model call is in progress.

Do not place provider credentials in the FAB, app prompt, launcher state file, dashboard, or run logs. Configure the upstream provider in the environment of the SuperLink that launches the Flower tasks. Existing SuperLink processes must be restarted to receive new environment values.

## Runtime details

- Flower and the AgentApp bundle target `flwr==1.39.0`.
- Each camera report carries `session_id`, `epoch`, source media time, UTC observation time, health, speaker evidence, and source revision. The broker rejects evidence that is future-dated, expired, or from another session generation.
- The Director ignores stale or cross-session reports, holds when camera evidence is missing or overlapping, and submits only controller-issued request metadata. The controller remains responsible for validating and executing every proposal.
- The Dashboard's mode is reported by the controller from live AgentApp heartbeats. A locally managed run ID alone is not treated as proof that an AgentApp is healthy.
- `.runtime/flower-agents.json` contains launcher-owned Flower run IDs; `stop` addresses only these IDs through `StopRunRequest`.
