# Noesis implementation contract

Current application contract for the controller, media path, control room, and Flower AgentApp with local orchestration by default. The controller is the local authority for session identity, source health, operator override, and OBS output. Editorial camera choices come from the AI Director; there is no rules-based speaker-ranking director.

## Topology and data boundary

The default local runtime is one persistent Flower AgentApp containing six independent AI roles. Four camera model calls and the critic refresh independently, with one outstanding lease per role. The director pins immutable copies of the latest five accepted reports and a fixed target source timestamp. Local inference defaults to a persistent client through the private loopback gateway to Nebius; native Flower model tasks remain optional. No SuperGrid login or cloud task dispatch is involved. The same session/model leases, manual controls, and validation apply in both modes.

In the optional hosted mode, the cloud Flower coordinator dispatches one-reply Grid jobs to six authenticated local SuperNodes: `camera-closeup1` through `camera-closeup4`, `critic`, and `director`. Each coordinator task has a 240-second budget. It fans out the four camera jobs and critic job concurrently. The critic uses the current measured round and bounded prior AI report history independently; it does not wait for or consume the four new camera responses. The Director receives the four current camera reports, current critic result, and a fresh snapshot. Native Flower Grid carries role tasks and replies. The local HTTP API carries measured source snapshots, heartbeats, and accepted AI results between those runs and the Noesis controller.

Camera model input contains whitelisted source measurements plus one JPEG of the assigned camera, captured at or before the request's source time (maximum age two seconds at issuance, 640×360, 128 KiB). The image is immutable for the lifetime of its request. Camera output adds `visual` with `person_visibility`, `board_visibility`, `activity`, and a bounded summary. Trusted image camera ID, timestamps, buffer epoch, dimensions, and SHA-256 accompany the report and must match its lease; image bytes never enter public state, events, or report history. Missing images produce explicit unknown visual evidence. Camera inputs exclude previous program explanations and editorial events. Full video, raw audio, and future annotations remain local. Background ASR may add bounded unassigned transcripts to critic/director context. The critic and director receive timestamped AI visual reports, actual shot history, and separate pending cuts. Exposure, face counts, and headset activity do not establish shot composition or participant visibility. Controller checks enforce schemas, provenance, epochs, target health, and manual/replay state; all editorial choices remain AI decisions.

## HTTP API

All routes are local to the Noesis service at `127.0.0.1:8765` by default. Request bodies reject unknown fields where shown. Errors use an HTTP error status and `{ "detail": "..." }`.

### Operator and media routes

| Route | Contract |
|---|---|
| `GET /api/state` | Full controller snapshot. |
| `GET /api/events` | Server-Sent Events named `state`, carrying a complete snapshot at a bounded cadence. |
| `POST /api/session/start` | `{ "input_mode":"synthetic\|ami", "output_mode":"preview\|obs", "start_s":0, "output_delay_s":5 }`. Defaults to synthetic/preview; delay is optional (0–15 seconds), preserving configured delay when omitted. |
| `POST /api/session/pause`, `/api/session/resume`, `/api/session/stop` | `{}`. Session transitions invalidate outstanding AI work. |
| `POST /api/session/seek` | `{ "time_s": number }`; starts a new replay epoch and invalidates old results. |
| `POST /api/control/override` | `{ "camera_id": string }`; latches manual control. |
| `POST /api/control/autopilot` | `{}`; explicitly releases the manual latch. |
| `POST /api/fault` | `{ "camera_id": string, "kind":"black\|freeze\|offline\|none", "duration_s":10 }`; test faults alter the shared source used by preview, health checks, and program output. |
| `GET /api/frame/{camera_id}.jpg`, `GET /api/program.jpg` | Current no-cache JPEGs. `camera_id` may be a source or `slate`. |
| `GET /api/program.mjpeg` | Persistent multipart JPEG stream, targeting 30 updates/s from captured source frames; actual source FPS is measured separately. |
| `GET /api/audio` | Seekable WAV for AMI program audio; `404` when unavailable. |
| `GET /program` | Full-screen program page for the OBS browser source, with one shared camera selection and continuous AMI audio. |
| `POST /api/obs/connect`, `POST /api/obs/setup` | `{}`; connect and read back OBS state, or prepare the `NOESIS_Program` browser-source scene. |

### Flower and model routes

| Route | Contract |
|---|---|
| `GET /api/agents/snapshot` | Atomic session, epoch, model epoch, source revision, compact camera features, and recent camera-agent observations for a coordinator round. |
| `POST /api/agents/heartbeat` | `{agent_id, role, runtime, run_id?, camera_id?, decision_mode?}`. Roles are `camera`, `critic`, or `director`; active AI workers report `decision_mode:"llm"`. |
| `POST /api/agents/observations` | `{camera_id, source_observation_revision, observation:{...}}`; rejects future, expired, unknown, or cross-session evidence. |
| `GET /api/ai/lease/{agent_id}` | Local continuous mode: one immutable source/evidence lease per authenticated role heartbeat, or `null` when unavailable. Director lease pins five response IDs and `target_media_time_s`; newer reports cannot replace them. |
| `GET /api/ai/round` | Current leased AI round or `null`. Includes `request_id`, session/override/model epochs, model identity, media time, observation revision, camera features, current program, recent events, and remaining deadline. |
| `POST /api/ai/report` | A camera or critic result with request/session/override/model provenance, `agent_id`, `role`, optional `camera_id`, source revision/time, model and response ID, latency, token counts, and role-specific `result`. |
| `POST /api/ai/decision` | The same provenance envelope from `director`, plus `evidence_response_ids` referencing exactly the current four camera reports and critic report. The result is `{action:"hold\|switch", camera_id, reason}`. |
| `POST /api/models/select` | `{ "profile":"kimi\|minimax" }`. Only configured profiles are selectable. Switching increments `models.epoch`, clears old inference results, and invalidates outstanding work without pausing playback. |

AI result provenance fields are `request_id`, `session_id`, `epoch`, `override_epoch`, `model_epoch`, `agent_id`, `role`, `source_revision`, `media_time_s`, `model`, `response_id`, `latency_ms`, `input_tokens`, `output_tokens`, and `result`. Camera reports also include `camera_id`. A director result also includes the five `evidence_response_ids`.

Role result schemas are exact JSON objects:

- Camera: `{ "recommendation":"take\|hold\|avoid", "confidence":number 0..1, "reason":string }`.

- Critic: `{ "assessment":"steady\|change\|wide", "reason":string }`.

- Director: `{ "action":"hold\|switch", "camera_id":"closeup1\|closeup2\|closeup3\|closeup4\|corner\|slate", "reason":string }`.

The controller accepts a director choice only after all four camera roles and the critic have reported for its pinned evidence set (or exact round in hosted mode), the AI sender has a current Flower heartbeat, the response is unique and unexpired, and the chosen source is healthy at the target timestamp. Buffered choices are rechecked immediately before airing. `slate` is allowed only when no healthy source is available. Manual override and session/model changes invalidate pending results.

## State snapshot

`GET /api/state` contains:

- `session`: `{id, epoch, status, input_mode, output_mode, time_s, duration_s}`.

- `mode`: `autopilot`, `manual`, or `degraded`. `autopilot` requires all six current AI-role heartbeats; a process or run ID alone is not proof of agent health.

- `program`: selected `camera_id`, scene, reason, cut time, and `decision_source` provenance.

- `broadcast`: delayed output time, input time, configured delay, fill/drain phase, readiness, buffer epoch/bytes, measured per-camera FPS, capture processing time, and pending cuts.

- `perception`: local speech/visual worker status, causal timestamped results, processing latency, and queue replacement counters.

- `shot_history`: bounded actual aired shots; future scheduled cuts are not labeled as aired history.

- `cameras`: five sources with measured health, status, speaker activity, energy, quality, observation age, and frame URL.

- `obs`: actual connection/recording status, retryable stop state, and output path when known.

- `models`: selected director/critic profile, shared model epoch, safe catalog entries `{id,label,model,available}`, and `roles` assignments for camera, critic, and director. The optional camera-only Flower model does not appear as a global profile. Each lease validates the assigned model independently.

- `flower`: `topology:"local|supergrid"`, `crew_mode`, inference transport, model verification status, six live-agent entries, latest safe inference result per role, and managed coordinator run metadata when available.

- `metrics`, bounded recent `events`, source availability, and top-level `observation_revision`.

`flower.model_status` is `configured_not_verified` until an accepted completed model response exists, `verified` after one, or `not_configured` when no selected profile is available. A role is verified only by its accepted result from the current session, override epoch, and model epoch. Never turn a requested, timed-out, rejected, or stale response into a successful result in the UI or logs.

`flower.grid_run` is launcher-provided metadata `{run_id, federation, status, sub_status, checked_at}` when present. A managed run ID or `Pending` state is not an inference result; only accepted response provenance proves that a role completed model work.

## Media and audio contract

`noesis/media.py` supplies the shared-clock source and implements the `MediaEngine` interface used by `noesis/app.py` and `noesis/controller.py`. Five video angles share one replay time. Camera cuts must not restart video or the AMI audio mix. The control-room Start gesture may unlock browser audio; the output page maintains the same continuous audio for OBS. Synthetic input is clearly labeled and has no AMI audio. `availability()` reports missing data and attribution; absent AMI files must never be represented as real footage.

Production defaults to five seconds of output delay. A bounded JPEG ring per camera stores source PTS and measured health. The program stream selects the frame at or before its output timestamp; audio follows that same delayed clock. Pause freezes both clocks, seek flushes and refills the rings, and EOF drains the remaining delayed footage before stopping OBS. Manual control immediately selects a buffered source and clears pending AI cuts. A late AI choice cannot change footage that already aired.

Local speech and visual workers have one replaceable pending job each. Four-second speech windows end no later than the observed input clock, visual jobs sample at most once per second, and old session/epoch results are discarded. Missing speech dependencies/cache degrade visibly; runtime never downloads models. Transcripts are fallible, untrusted data, not operator instructions. Reference annotations are never used for live decisions.

## Safety and ownership invariants

- The AI Director alone makes normal editorial camera choices. Health handling may move away from a failed source; it never chooses a shot by ranking speaker scores.

- Recheck target health and the current session/model/override epochs immediately before execution.

- Manual selection stays latched until the operator explicitly resumes autopilot. No late AI result may override it.

- Pause, seek, stop, source transition, model switch, or stale Flower heartbeat invalidates pending work.

- If an AI round expires or evidence is incomplete, retain a healthy current shot. Health recovery may select a currently healthy alternative; use the slate only when all cameras are unavailable.

- Failed continuous requests release only their matching lease and control epochs. Do not reuse a failed director evidence set until a report changes, or extend evidence freshness to accommodate slow inference. Admit director work only with a useful response budget before both evidence expiry and the target's broadcast deadline.

- Agent prompts treat measured audio as primary speaker evidence and still images as evidence of framing and visible scene content. Historical decision explanations cannot override current observations. A single frame cannot prove motion or speech, and camera confidence does not rank editorial relevance.

- Confirm actual OBS recording state after start/stop. A disconnected status is unknown, not confirmation of stop. Keep Stop retryable if acknowledgment was lost.

- Keep source credentials out of state, browser code, FAB prompts, events, and public reports. The model gateway is the allowlisted provider boundary.

- Label synthetic feeds, preview output, recorded AMI replay, and test faults honestly.

See [Flower setup and topology](docs/FLOWER.md) for the current launcher, and [validation status](docs/VALIDATION.md) for evidence that applies to this architecture.
