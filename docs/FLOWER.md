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

## Nebius model profiles

Two private profiles are supported: `kimi` and `minimax`. Configure these variables in the ignored `.env`:

- `NEBIUS_KIMI_API_ENDPOINT`, `NEBIUS_KIMI_MODEL`, `NEBIUS_KIMI_API_KEY`
- `NEBIUS_MINIMAX_API_ENDPOINT`, `NEBIUS_MINIMAX_MODEL`, `NEBIUS_MINIMAX_API_KEY`

The endpoint must be the full Open Responses URL ending in `/responses`. The supplied Nebius deployments use `https://api.tokenfactory.tf-ca1.nebius.com/v1/responses`. Keys stay out of the AgentApp FAB, run prompt, dashboard, and reports; `ex.txt` is also ignored.

```powershell
.\start.ps1 -OBS -ModelProfile kimi
# In another terminal:
.\.venv\Scripts\python.exe scripts/verify_nebius.py --profile kimi
```

Stop that launcher with Ctrl+C before selecting MiniMax:

```powershell
.\start.ps1 -OBS -ModelProfile minimax
.\.venv\Scripts\python.exe scripts/verify_nebius.py --profile minimax
```

Switching profiles requires a new SuperLink process because its model workers inherit the upstream configuration. The launchers reject reuse when the running service's recorded profile does not match. The local profile record contains metadata and a key fingerprint, never the key itself.

The AgentApp calls `client.responses.create` with Flower's injected `FLWR_RUNTIME_BASE_URL` and `FLWR_RUNTIME_API_KEY`. Flower forwards each model task to the configured Nebius Responses endpoint using `FLWR_MODEL_API_ENDPOINT` and `FLWR_MODEL_API_KEY`. No direct Nebius inference call is made by the AgentApp.

## Fast camera control and slower editorial policy

Hosted inference takes longer than the fast camera-switching window. The Director therefore keeps camera proposals rules-based, with explicit `decision_source: rules`. A separate worker makes at most one model call at a time, with a 25-second client timeout and a 30-second controller lease. It requests policy roughly every 10 seconds while replay is running in autopilot.

The model receives compact causal camera-agent observations and recent event types, never raw video, audio, transcripts, or credentials. Its strict JSON result can choose only:

- A minimum shot duration from 4 to 8 seconds.
- `hold` or `wide` for overlapping speakers.
- A short explanation.

A valid result becomes a 20-second policy. The local controller applies it to current evidence and checks the target again at camera-cut commit. Camera health recovery bypasses the minimum shot duration. Manual override, pause/seek/session changes, director heartbeat loss, or policy expiry remove the policy. Failures retain local rules.

Model response validation, policy acceptance, and actual policy-guided cuts are separate events. The dashboard shows an active model policy only after acceptance; otherwise it explicitly reports local rules or an unverified configuration. Safe model response IDs, token counts, latency, and profile identity are retained for the rehearsal report.

`verify_nebius.py` records a bounded synthetic rehearsal, requires an accepted model policy and at least one policy-guided cut, checks black-camera recovery and manual override, and decodes the OBS recording. It saves `.runtime/nebius-<profile>-acceptance.json`; `--preview-only` skips OBS. Reports and recordings stay local.

Both profiles passed on 29 September 2026: three accepted policies and four policy-guided cuts per rehearsal. Final Kimi calls took about 3.5 seconds and MiniMax calls 4.0–5.5 seconds; an earlier cold Kimi call took 22.7 seconds. Recovery and manual override passed with each profile. See [full measured results and limits](VALIDATION.md).

Flower 1.39.0 separately requests conversation titles using a hardcoded `openai/gpt-5-nano` model. The provided Nebius deployments reject that model with a nonfatal 404. Successful director responses are verified by their own model identity, response ID, and accepted policy events; title errors do not indicate that Kimi or MiniMax inference failed.

## Runtime details

- Flower and the AgentApp bundle target `flwr==1.39.0`.
- Each camera report carries `session_id`, `epoch`, source media time, UTC observation time, health, speaker evidence, and source revision. The broker rejects evidence that is future-dated, expired, or from another session generation.
- The Director ignores stale or cross-session reports, holds when camera evidence is missing or overlapping, and submits only controller-issued request metadata. The controller remains responsible for validating and executing every proposal.
- The Dashboard's mode is reported by the controller from live AgentApp heartbeats. A locally managed run ID alone is not treated as proof that an AgentApp is healthy.
- `.runtime/flower-agents.json` contains launcher-owned Flower run IDs; `stop` addresses only these IDs through `StopRunRequest`.
