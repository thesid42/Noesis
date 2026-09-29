# Flower SuperGrid setup

Noesis's target runtime is collaborative Flower Agents on the authenticated `@thesid42/noesis` SuperGrid federation. The local source clock, HTTP controller, and OBS bridge remain on the demo host; native Flower Grid messages carry bounded tasks and replies between the coordinator and agents.

## Six AI roles

The coordinator is a bounded Flower AgentApp run with a 240-second task budget. It dispatches one-reply jobs to six local SuperNodes. The four camera jobs and critic job start in parallel. The critic evaluates the current measured round and bounded prior AI report history independently; it does not wait for the four fresh camera results or consume them as inputs. The Director then uses all four current camera results, the current critic result, and a fresh snapshot.

| Flower identity | Work |
|---|---|
| `camera-closeup1` … `camera-closeup4` | Independently assess measured health, speaking state, energy, quality, and age for one assigned close-up. Return `take`, `hold`, or `avoid` with a reason and confidence. |
| `critic` | Assess the current measured round plus bounded prior AI report history as `steady`, `change`, or `wide`. It runs independently of the fresh camera jobs and does not pick a camera. |
| `director` | Read all four camera reports, the critic report, and a fresh source snapshot; choose `hold` or a current healthy camera and submit the final editorial decision. |

All six roles make model calls through the Flower-injected Responses runtime. The application sends compact numeric and categorical observations only. The models do not receive raw camera frames, raw audio, transcripts, or annotation labels, so Noesis makes no claim of semantic visual or speech understanding. The controller accepts the Director choice only when it cites the four camera replies and the parallel critic reply with matching current-round provenance.

Flower Grid handles role-to-role job/reply traffic. The Noesis HTTP broker only supplies measured source snapshots and receives agent heartbeats and results; it is not used as a substitute for Grid messaging. The controller is deterministic about schema, provenance, time, health, and manual-control checks, but it does not use a speaker-score rule to pick the editorial shot. If AI work is missing or late, it keeps a healthy current shot. If the on-air source fails, the health guard may move to a healthy source.

## Prerequisites and private configuration

Use Python 3.12 and install the pinned Flower extra from the project root:

```powershell
uv sync --python 3.12 --extra flower --extra dev
Copy-Item .env.example .env  # skip if .env already exists
```

In `.env`, configure both Nebius profiles with their full `/v1/responses` endpoints, model IDs available to your account, and API keys. The current launcher expects both profiles because the control room supports a live Kimi/MiniMax selector:

```text
NEBIUS_KIMI_API_ENDPOINT=
NEBIUS_KIMI_MODEL=
NEBIUS_KIMI_API_KEY=
NEBIUS_MINIMAX_API_ENDPOINT=
NEBIUS_MINIMAX_MODEL=
NEBIUS_MINIMAX_API_KEY=
```

Use the exact endpoint and model identifiers for your provider account; the public repo does not include keys. `.env` is ignored by Git. Do not paste keys into a prompt, browser, AgentApp task, or demo report.

The Flower CLI must already have an authenticated `supergrid` connection with permission to use the `@thesid42/noesis` federation. The launcher uses that saved connection and does not print or copy its authentication tokens. The SuperNodes use generated per-node keys under ignored `.runtime/supergrid/keys/`.

## Register nodes and start the demo

Register the six node identities once:

```powershell
.\.venv\Scripts\python.exe scripts\supergrid_agents.py setup
.\.venv\Scripts\python.exe scripts\supergrid_agents.py status
```

Registration adds these identities to the configured federation. `status` shows current SuperGrid node status; it does not establish that a model response or AI decision has completed.

Start the full runtime with Kimi selected by default:

```powershell
.\.venv\Scripts\python.exe scripts\run_demo.py --obs --model-profile kimi --duration-s 240
```

The launcher starts the local controller and model gateway, starts six local Flower SuperNodes, waits for all of them to be online, and submits a coordinator task capped at 240 seconds. The task stays below the event's five-minute per-task limit. The launcher can renew completed coordinator tasks until its overall `--duration-s` budget ends; the default budget is one hour. Keep the terminal open. Ctrl+C stops the coordinator run and services owned by this launcher.

Open **http://127.0.0.1:8765**. Start a Synthetic session for generated, visibly labeled test feeds, or an AMI session when its prepared media is available. Choose OBS output when the real OBS program scene is ready; omit `--obs` to use the control-room preview. The `/program` page is the single OBS browser source and preserves AMI mix audio across cuts. Starting an AMI session from the control room also gives the browser its user gesture to unlock audio.

The launcher configures the loopback model gateway with the two configured provider targets. The control room has a model selector and switches invalidate outstanding responses from the prior model epoch without pausing the session. Keep Kimi selected for this demo: MiniMax was not run or verified. Flower-injected credentials reach the loopback gateway, while Nebius credentials stay in the gateway process. Provider keys are never part of the FAB, Grid task payload, API state, or result records.

The Windows wrapper is also available:

```powershell
.\start.ps1 -OBS -ModelProfile kimi
```

`start.ps1` starts the same SuperGrid launcher. It does not provide a local-only AI substitute. To configure the project's portable OBS before the first OBS run, use `scripts/setup_obs.py` and `scripts/configure_obs.py` as described in the README.

## What counts as a verified result

Check both the managed Grid run and the controller state. The dashboard reports all six role heartbeats, a coordinator run ID/status when available, model profile/epoch, and accepted model results with response IDs and token/latency metadata. A registered node or submitted task is not proof that its worker came online or returned a valid result. `flower.model_status` remains `configured_not_verified` until a completed model response has been accepted; each role result is displayed only when current for the session, override, and model epoch.

For the current integration status, see [validation](VALIDATION.md). All six nodes are online in the authenticated federation. Kimi run `96528684490097715` finished `completed` in 245.545 seconds, with six accepted role responses and a Director choice of Closeup3. The first round expired at its 30-second deadline; a later round completed in about 23 seconds. The launcher renewed into run `3067815238044157620`, which subsequently reached Running. No MiniMax run was made.

## Hackathon submission requirements

The organizer's public requirements JSON was checked on 29 September 2026. The required submission includes collaborative Flower Agents on SuperGrid, an AgentApp published to Flower Hub, a team registration form with a team description and GitHub repository, and a 3–5 minute demo. Individual tasks must stay at or below five minutes. Endeavor is optional.

The code and launcher implement the SuperGrid topology and a 240-second task budget. All six nodes are online and Kimi has a successful live round; the renewed run also reached Running. The AgentApp is prepared locally but has not been published to Flower Hub. Completing the team form and final demo remain pending.

## Local Flower Hub package review

The AgentApp package is prepared for local review and deliberately remains unpublished. Run:

```powershell
.\.venv\Scripts\python.exe scripts\prepare_hub.py
```

This stages the files under ignored `.runtime/hub/noesis-agents/`, checks Flower's source-file rules, scans the seven source files for configured secret values, builds a FAB locally, and writes source hashes and FAB identity to `.runtime/hub/review.json`. The latest preparation reported seven files and zero secret matches. It does **not** publish to Flower Hub; publication remains pending.

For the explicit requirements checklist and outstanding submission items, see [hackathon requirements](HACKATHON_REQUIREMENTS.md).
