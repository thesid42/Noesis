# Flower runtimes and setup

Noesis defaults to Flower's native local runtime. A single local AgentApp run orchestrates six logical AI roles: four camera reviewers and a critic run in parallel, then the Director receives those reports and makes the editorial choice. No Flower account, hosted coordinator, or SuperNode registration is needed to use this mode.

Model inference is remote: the local AgentApp sends compact source measurements and role reports through the Noesis loopback gateway to the configured Nebius endpoint. Orchestration and media/OBS control remain local. The model prompts do not contain raw video, raw audio, transcripts, or future annotations.

## Roles and decision flow

| AI role | Work |
|---|---|
| `camera-closeup1` … `camera-closeup4` | Independently assess measured health, speaking state, energy, quality, and age for one assigned close-up. Return a recommendation with a reason and confidence. |
| `critic` | Assess the current measured round and bounded prior AI report history independently of the four current camera jobs. It provides editorial context and does not pick a camera. |
| `director` | After the camera and critic results arrive, use their reports and a fresh source snapshot to choose `hold` or a healthy camera. |

The controller validates the real model responses, request/session/model/override provenance, deadlines, and the target's current health before executing a choice. Manual camera control remains latched until explicit resume. If an AI response is missing or late, the controller holds a healthy current shot; if the on-air source fails, the health guard can move to a healthy source. The controller does not replace AI with speaker-score ranking.

## Run the local runtime

Use Python 3.12 and `uv` from the project root:

```powershell
uv sync --python 3.12 --extra flower --extra dev
Copy-Item .env.example .env  # skip if .env already exists
```

Set the Kimi endpoint, model ID, and key in the ignored `.env`. The dashboard defaults to Kimi. The MiniMax profile remains available in the selector when configured, but it was not run or verified for this demo. Provider keys stay in the local gateway process; they are not included in browser state, Flower task payloads, or reports.

Start the local Flower run and control room:

```powershell
.\start.ps1 -OBS
```

On macOS/Linux, use `./start.sh --obs`. Omit the OBS switch for preview output. The launchers use the local Flower runtime by default, the model gateway on loopback port 8770, and the Noesis control room on port 8765. Flower's local SuperLink uses loopback port 8000 and fleet API port 9092. Keep the terminal open; Ctrl+C stops services owned by the launcher.

The dashboard labels the active orchestration as **Local Flower Run** and shows the AgentApp run ID/status when the runtime reports them. Its model verification indicator reflects completed, accepted model responses, not merely a configured profile or running Flower process.

Local mode defaults to `--inference-transport gateway`. One persistent async client in the Flower AgentApp sends requests to the authenticated loopback gateway, avoiding a new Flower model subprocess for each request. The six AI roles, strict result schemas, provenance, replay epochs, deadlines, and source-health checks are unchanged. Provider keys remain in the gateway; the AgentApp receives only a local routing token through its process environment. State reports `inference_transport: gateway`. Opt into `--inference-transport flower` to use native Flower model tasks locally. Hosted SuperGrid automatically uses `flower`; an explicit gateway option is rejected with `--runtime supergrid`.

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

The event separately requires collaborative Flower Agents on SuperGrid and an AgentApp published to Flower Hub. Local Flower mode alone does not establish the SuperGrid requirement. The Hub package was built and secret-checked locally from seven files, but it was deliberately not published. No MiniMax inference was run.

## Local Flower Hub package review

The AgentApp can be prepared for review without publishing:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_hub.py
```

This stages files under ignored `.runtime/hub/noesis-agents/`, checks Flower's source-file rules, scans for configured secret values, builds a local FAB, and records source hashes in `.runtime/hub/review.json`. The latest preparation reported seven files and zero secret matches. It does **not** publish to Flower Hub; keep the package local unless publication is separately requested.

For current test and media evidence, see [validation](VALIDATION.md). For the full event checklist, see [hackathon requirements](HACKATHON_REQUIREMENTS.md).
