# Noesis implementation contract

Shared contract for the advisor/executor build. Root coordinates integration. Python package: `noesis`. Web assets: `noesis/static`. Use FastAPI and vanilla HTML/CSS/JS. Avoid a frontend build step.

## HTTP API

- GET `/api/state`: full snapshot described below.
- GET `/api/events`: Server-Sent Events containing full snapshots, event name `state`; frontend may fall back to polling state.
- POST `/api/session/start`: `{input_mode: "synthetic"|"ami", output_mode: "preview"|"obs", start_s: 0}`.
- POST `/api/session/pause`, `/api/session/resume`, `/api/session/stop`: `{}`.
- POST `/api/session/seek`: `{time_s: number}`.
- POST `/api/control/override`: `{camera_id: string}`; latches manual.
- POST `/api/control/autopilot`: `{}`; explicitly resumes automatic control.
- POST `/api/fault`: `{camera_id: string, kind: "black"|"freeze"|"offline"|"none", duration_s: 10}`.
- GET `/api/frame/{camera_id}.jpg`: current JPEG, no cache.
- GET `/api/program.jpg`: selected program JPEG, no cache (preview mode must be visibly labeled).
- GET `/api/audio`: audio WAV for AMI when available; seekable HTTP FileResponse.
- GET `/program`: clean output page with program video/image and continuous audio, suitable for OBS browser source.
- POST `/api/obs/connect`: reconnect OBS using server environment configuration.
- POST `/api/obs/setup`: create Noesis browser-source scene(s), preserving unrelated scenes.
- GET `/api/agents/snapshot`: compact causal observations plus atomic `session` identity/time for real Flower agents. Transitions reject agent reads until fresh media evidence is available.
- POST `/api/agents/heartbeat`: `{agent_id, role, runtime, run_id?, camera_id?}`.
- POST `/api/agents/observations`: camera-agent report including camera_id and source observation revision.
- POST `/api/agents/proposal`: validated director proposal. Request deadline/ID originate at controller.
- GET `/api/agents/request`: current pending director request or null, including request_id, session_id, epoch, override_epoch, observation_revision, state and deadline_remaining_ms.
- GET `/api/agents/editorial`: one leased policy request or null. Requires a live LLM director and recent camera-agent evidence. Includes the controller-issued request/session/override metadata, current camera evidence, and a 30-second deadline.
- POST `/api/agents/editorial`: `{agent_id, request_id, session_id, epoch, override_epoch, policy_id, min_shot_s, overlap_mode, reason, model, response_id, latency_ms, input_tokens, output_tokens}`. Strictly validates a completed model result; `min_shot_s` is 4–8 and `overlap_mode` is `hold|wide`. Returns `{ok, policy}`. Accepted policy lasts 20 seconds and is rechecked against current evidence at each cut.

Mutations return latest snapshot or a documented `{ok,...}` object; failures use normal HTTP error status and `{detail}`. Credentials read from environment, never returned in API responses. Bind local host by default.

## State fields

`session`: `{id, epoch, status: idle|running|paused|stopped, input_mode: synthetic|ami, output_mode: preview|obs, time_s, duration_s}`.

`mode`: `autopilot|manual|degraded` (no Flower means degraded deterministic directing; idle may be autopilot).

`program`: `{camera_id, scene, reason, last_cut_s, decision_source, editorial_policy_id?}`. Rules camera selection and model editorial policy are separate provenance fields.

`cameras`: list of `{id, name, participant, healthy, status, speaking, energy, quality, age_ms, frame_url}`. IDs: `closeup1`, `closeup2`, `closeup3`, `closeup4`, `corner`; participant mapping A,C,D,B respectively. `slate` is last-resort program target.

`obs`: `{connected, status, error?, recording, stop_pending, output_path?}`. A disconnected recording state is unavailable, not proof of a stopped recorder. `stop_pending` keeps Stop retryable after reconnecting and blocks a new session until acknowledgment.

`flower`: `{status, transport, model?, model_status, editorial_policy?, last_model_result?, decision_modes, error?, agents: [{agent_id, role, runtime, decision_mode, run_id?, camera_id?, age_ms, healthy}]}`. Model status distinguishes configured from verified; last result contains only model identity, response ID, elapsed latency, and token counts. Never claim Flower running from local fallback code.

`metrics`: `{cuts, fallbacks, rejected_proposals, model_timeouts, last_recovery_ms?, editorial_requests, editorial_policies, editorial_timeouts, policy_guided_cuts}`. Reset at each successful session start. `model_timeouts` is the legacy fast director-request timeout counter; `editorial_timeouts` tracks the separate model-policy lease. `last_recovery_ms` measures detector declaration to controller selection; injection-to-selection timing is measured independently by the rehearsal script and includes detector debounce.

`events`: bounded list (newest first) of `{id, session_id, time_s, kind, source, message, camera_id?}`. The active trace is cleared at each successful session start; export it before beginning another session when retaining a run record.

`data`: `{ami_available, missing_files: [], credits}`.

## Media module contract

`noesis/media.py` exports `MediaEngine(data_dir: Path | None = None)`.

Synchronous methods (backend may call expensive sampling via `asyncio.to_thread`):
- `start(input_mode="synthetic", start_s=0.0)`
- `pause()`, `resume()`, `stop()`, `seek(time_s)`
- `inject_fault(camera_id, kind, duration_s=10.0)`
- `snapshot()` -> `{time_s, duration_s, input_mode, status, cameras: [...], observation_revision}`. Camera entries include fields above and frame_url. Sampling must stay causal to a monotonic replay clock.
- `frame_jpeg(camera_id)` -> JPEG bytes; applies actual injected faults to same source used by program and detector.
- `audio_path()` -> Path or None.
- `availability()` -> `{ami_available, missing_files, credits}`.

Provide visibly labeled generated test feeds when AMI is absent, not a fake AMI demo. Use OpenCV and NumPy for real media if installed; simulation can use Pillow. Avoid clock updates depending on browser reads. Guard shared state and bound queues/cache. Never call future annotation labels during perception.

## Ownership

- Backend executor: app/API, controller/state/deadline logic, OBS bridge, backend tests. Do not edit media.py or static assets. Temporary internal fake media test fixture is fine; no fake Flower success.
- Frontend executor: static assets and /program UI (served by backend), frontend behavior. Do not edit backend modules.
- Media/Flower executor assigned next: media.py, AMI download/preparation scripts, actual Flower apps and launch instructions; coordinate file ownership with root.
- Root: dependency/project scaffold, integration fixes, data acquisition, docs, end-to-end verification.

## Critical invariants

Local fallback never waits for LLM. Emergency cuts bypass normal minimum hold. Manual override blocks every automatic cut until resume. Replays/seeks invalidate old decisions. Reject malformed, expired, duplicate, wrong-session or wrong-epoch proposals. Only one controller writes to OBS. Confirm actual OBS result. Always label simulated inputs, preview output, and non-Flower fallback honestly.

Nebius profiles are selected at launcher startup. Only SuperLink receives the selected upstream API key; AgentApps use Flower-injected runtime credentials. Profile changes require restarting the owned SuperLink. Policy inference is a separate single-call worker from heartbeat and rules proposals. Manual override, pause, seek, session transitions, director heartbeat loss, or expiry invalidate policy. Keys, `.env`, `ex.txt`, generated FABs, and model test reports stay out of Git.
