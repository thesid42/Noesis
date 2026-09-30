# Flower runtimes and setup

Noesis defaults to Flower's native local runtime. A single local AgentApp run orchestrates six logical AI roles: four camera reviewers and a critic refresh independently, while the Director pins their latest recent reports and a current source snapshot. Each role has at most one request in flight. No Flower account, hosted coordinator, or SuperNode registration is needed to use this mode.

Model inference is remote: the local AgentApp sends source measurements and role reports through the Noesis loopback gateway. Each camera call also sends one bounded, timestamp-pinned JPEG to its configured model provider (currently Qwen3.5-9B through Flower). Critic/director calls use Nebius Kimi and receive structured visual assessments rather than images. Orchestration and media/OBS control remain local. Full video, raw audio, and future annotations are not sent. Optional local transcription contributes recent unassigned speech text; lightweight frame analysis contributes technical measurements. Image assessment uses the existing parallel camera calls, with no additional serial inference stage.

## Roles and decision flow

| AI role | Work |
|---|---|
| `camera-closeup1` … `camera-closeup4` | Independently assess measured health, speaking state, energy, quality, and age for one assigned close-up. Return a recommendation with a reason and confidence. |
| `critic` | Assess current source evidence, recent transcript, actual aired shot history, and prior reports independently. It provides editorial context and does not pick a camera. |
| `director` | Pin the latest four camera reports and critic report plus a fixed source timestamp, then choose `hold` or a healthy camera for that timestamp. |

An optional `NOESIS_CAMERA_MODEL=qwen/qwen3.5-9b` override routes only the four camera roles to Flower's public Responses endpoint using `FLWR_MODEL_API_KEY`. Set `NOESIS_CAMERA_REASONING_EFFORT=none` for non-thinking camera responses. The director and critic retain the selected Nebius profile. Expected model identity is pinned per role, and the director may combine reports from the two configured models. Provider keys are held only by the local gateway.

The controller validates the real model responses, request/session/model/override provenance, deadlines, and the target's current health before executing a choice. Manual camera control remains latched until explicit resume. If an AI response is missing or late, the controller holds a healthy current shot; if the on-air source fails, the health guard can move to a healthy source. The controller does not replace AI with speaker-score ranking.

Role prompts prioritize current measured speaker evidence over a camera's visual appeal. Image assessments can identify an empty assigned view or relevant board interaction, but cannot establish motion or speech from a single frame. `hold` from a camera means standby/no strong takeover recommendation; only the director's `hold` preserves the projected program shot. Historical program and shot explanations are excluded from model inputs, while camera IDs and shot times remain for continuity. Pinned reports retain their observation times, and camera confidence is not a shot-ranking score.

Continuous workers report terminal failures to `/api/ai/lease/failure` using their exact request and control epochs. Only the matching lease can be released; stale or duplicate receipts cannot cancel newer work or change accepted decisions. Failure reporting and short retry backoff replace waiting for an abandoned lease to expire. The gateway cancels its pending provider HTTP request if the local worker disconnects. These paths add no inference calls or serial model stage.

Director admission requires at least three seconds for inference plus half a second for submission before both evidence expiry and the buffered target's output deadline. This is a minimum useful budget, not a latency guarantee. If evidence is too old, the controller waits for fresh reports without consuming their response IDs. The fifteen-second evidence lifetime and rejection of already-aired targets still apply. A provider taking six or seven seconds will require a longer output delay than five seconds, even with healthy orchestration.

## Run the local runtime

Use Python 3.12 and `uv` from the project root:

```powershell
uv sync --python 3.12 --extra flower --extra dev --extra speech
.\.venv\Scripts\python.exe scripts\prepare_speech.py
Copy-Item .env.example .env  # skip if .env already exists
```

Set the Kimi endpoint, model ID, and key in the ignored `.env`. The dashboard defaults to Kimi. The MiniMax profile remains available in the selector when configured, but it was not run or verified for this demo. Provider keys stay in the local gateway process; they are not included in browser state, Flower task payloads, or reports.

Start the local Flower run and control room:

```powershell
.\start.ps1 -OBS
```

On macOS/Linux, use `./start.sh --obs`. Omit the OBS switch for preview output. The launchers use the local Flower runtime by default, the model gateway on loopback port 8770, and the Noesis control room on port 8765. Flower's local SuperLink uses loopback port 8000 and fleet API port 9092. Keep the terminal open; Ctrl+C stops services owned by the launcher.

The dashboard labels the active orchestration as **Local Flower Run** and shows the AgentApp run ID/status when the runtime reports them. Its model verification indicator reflects completed, accepted model responses, not merely a configured profile or running Flower process.

Local mode defaults to `--inference-transport gateway`. One persistent async client in the Flower AgentApp sends requests to the authenticated loopback gateway, avoiding a new Flower model subprocess for each request. The six AI roles, strict result schemas, provenance, replay epochs, deadlines, and source-health checks are unchanged. Provider keys remain in the gateway; the AgentApp receives only a local routing token through its process environment. State reports `inference_transport: gateway` and `crew_mode: continuous_per_role`. Camera leases refresh no faster than once a second, critic leases once every two seconds; actual cadence also depends on inference time. Reports expire 15 seconds after their source was captured, not 15 seconds after the response arrives. Director evidence IDs remain immutable even when newer reports arrive. Opt into `--inference-transport flower` to use native Flower model tasks locally. Hosted SuperGrid automatically uses `flower`; an explicit gateway option is rejected with `--runtime supergrid`.

To inspect or stop the local launcher from another terminal:

```powershell
.\.venv\Scripts\python.exe scripts\local_agents.py status
.\.venv\Scripts\python.exe scripts\local_agents.py stop
```

A live Kimi smoke test is available with `scripts/verify_nebius.py --profile kimi --preview-only`. It uses model credits, replaces the current replay, requires all six AI responses plus a model-directed cut, and checks health recovery and manual override. No model call is needed just to open the dashboard; inference starts when an automatic replay is running.

## Hosted SuperGrid option

The hosted runtime is still available but is opt-in. It uses an authenticated Flower `supergrid` connection, the `@thesid42/noesis` federation, and six registered local SuperNodes. Set up the nodes once, then choose the hosted runtime explicitly:

```powershell
.\.venv\Scripts\python.exe scripts\supergrid_agents.py setup
.\.venv\Scripts\python.exe scripts\run_demo.py --runtime supergrid --obs --model-profile kimi --duration-s 240
```

The hosted coordinator uses a bounded task of at most 240 seconds, below the event's five-minute task cap. This path requires a Flower account and permission for the federation; it is not required to start the default local runtime. The control room identifies this mode as **SuperGrid Run**.

### Previous hosted verification

This is historical evidence from the earlier hosted mode, not a statement that the current default is using SuperGrid. All six authenticated federation nodes came online. Kimi coordinator run `96528684490097715` completed in 245.545 seconds and returned accepted responses from all six roles; the Director chose Closeup3. The rehearsal passed manual override and recovered from a source fault in 984 ms. A later hosted run was active during the final short AMI replay: four camera reports arrived at 17–19 seconds, but a Director decision did not arrive before the 20-second media reached EOF. That hosted replay did not prove AMI AI cuts before EOF. The newer local run did: all six responses and one AI cut were accepted within the clip; see [current validation](VALIDATION.md).

The event separately requires collaborative Flower Agents on SuperGrid and an AgentApp published to Flower Hub. Local Flower mode alone does not establish the SuperGrid requirement. [Noesis v0.2.0 is published on Flower Hub](https://flower.ai/apps/thesid42/noesis-agents). The seven-file source package passed the configured-secret scan, the CLI confirmed a successful upload, and the public listing was verified. No MiniMax inference was run.

## Flower Hub package

The published app is `@thesid42/noesis-agents`, version `0.2.0`. It contains three Python source files, README, LICENSE, `.gitignore`, and `pyproject.toml` (105,268 bytes total). Credentials, AMI footage, and recordings are excluded. Publication distributes the AgentApp; the studio controller, media sources, and trusted SuperNodes must still be configured for a hosted run.

The AgentApp can be prepared for review without publishing:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_hub.py
```

This stages files under ignored `.runtime/hub/noesis-agents/`, checks Flower's source-file rules, scans for configured secret values, builds a local FAB, and records source hashes in `.runtime/hub/review.json`. The latest preparation reported seven files and zero secret matches. It does **not** publish to Flower Hub; keep the package local unless publication is separately requested.

For current test and media evidence, see [validation](VALIDATION.md). For the full event checklist, see [hackathon requirements](HACKATHON_REQUIREMENTS.md).
