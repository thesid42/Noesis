# Validation status

Evidence updated 29 September 2026. Generated reports, recordings, keys, and run manifests stay in ignored local paths.

The current local implementation includes independent AI role updates, background speech/frame observations, bounded aired shot history, and a configurable broadcast buffer (five seconds by default). The [latest performance report](PERFORMANCE.md) records a real mixed-model AMI run: four Qwen3.5-9B camera agents through Flower with reasoning disabled, and Kimi critic/director through Nebius. All six roles returned accepted responses; three director decisions produced one aired cut, with no missed broadcast deadlines in that run. Qwen camera latency had a 1.891-second median; Kimi director latency had a 2.469-second median. First aired AI cut was about 10.7 seconds after start, including bootstrap and output delay.

The 25.166-second OBS recording includes silent buffer fill and delayed continuous AMI audio. Measured audio offset was about 5.35 seconds relative to the source mix. OBS confirmed recording stopped after output drained. Setup now refreshes the existing Noesis browser source to prevent cached player code from retaining the old audio clock, and refuses setup while recording. The recording is `recordings/verified-qwen-delayed-ami.mp4`; this is actual mixed-model output with an AI cut, not a synthetic timing simulation. Fine lip-sync and longer editorial evaluation remain unverified.

The first all-Kimi buffered test missed one deadline; five seconds is not a latency guarantee. The UI offers longer delays. Buffer memory peaked near 12 MiB, and throughput reflects the AMI sources' mixed native frame rates. MiniMax was not run. Subsequent publication is confirmed: [Noesis v0.2.0 on Flower Hub](https://flower.ai/apps/thesid42/noesis-agents), seven source/configuration files (105,268 bytes), zero configured-secret matches, successful CLI upload, and verified public README. This publication check did not run model inference or replay media.

The final regression suite passes **104 tests**. It covers fixed director targets and immutable evidence, per-role model/reasoning routing and verification, credential isolation, stale acquisition timestamps, pause/seek/EOF buffering, missing sources, manual queue invalidation, causal perception, and OBS refresh guards. JavaScript syntax and Git whitespace checks pass. Configured-secret scanning found no matches in public project files.

Earlier checks below are historical observations, not the current model assignment or scheduling architecture.

## Current local Flower checks

The default launcher now runs six AI roles in one native local Flower AgentApp, using remote Nebius Kimi inference. Verified local run `10720407148437064032` returned accepted real model responses from all six roles. There is no hosted queue or Grid dispatch in this mode.

| Check | Observed result |
|---|---|
| Regression suite | **51 passed**, including parallel role execution, incomplete-evidence rejection, HTTP/model wall-time cancellation, credential routing, and deployment reporting. |
| Synthetic Kimi round | All six accepted responses and an actual camera cut in **11.469 seconds** from replay start; director inference took 2.718 seconds. |
| Source health recovery | **985 ms** in the synthetic check. |
| Manual override | Passed. |
| Short AMI Kimi round | All six roles responded; first director response observed at **12.266 seconds**. **One AI camera cut** before EOF at 19.933 seconds. |
| AMI audio | `audio/wav` range request succeeded with HTTP 206. Previous browser audibility and OBS audio-recording measurements remain below; those were not remeasured in this local preview test. |
| MiniMax | Not run, per user request. Optional UI profile remains supported. |
| Static checks | Python compilation, JavaScript syntax, Bash syntax, and Git whitespace checks passed. |

These are individual test timings, not a latency guarantee. The earlier successful hosted round took about 23 seconds. At the time of that earlier check, models received features only. The current optional perception workers also supply recent transcript text and timestamped frame measurements.

Non-secret live reports are saved locally in `.runtime/local/kimi-acceptance.json` and `.runtime/local/ami-acceptance.json`. To repeat the synthetic acceptance test with the local launcher running, use `scripts/verify_nebius.py --profile kimi --preview-only`; it replaces the current replay and uses model credits.

## Historical hosted SuperGrid checks

| Check | Observed result |
|---|---|
| Regression suite | **42 passed**. |
| Authenticated federation | All six registered Noesis SuperNodes were online during the hosted proof in `@thesid42/noesis`. |
| Kimi SuperGrid inference | **Passed.** Cloud coordinator run `96528684490097715` finished `completed` in 245.545 s and produced accepted responses from all six AI roles; the Director selected Closeup3. The run stayed within the five-minute event limit. Safe role provenance is in `.runtime/supergrid/kimi-acceptance.json` and `.runtime/supergrid/run-history.json`. |
| Kimi round timing | The first 30-second round expired. A later round completed in about 23 seconds; individual Kimi model calls took 4.3–7.0 seconds. |
| Health and manual guards | The Kimi rehearsal recovered from a source fault in 984 ms and passed the manual override check. |
| AMI/OBS output smoke test | Five healthy feeds; video finalized at EOF. Recording length 20.03 s; audio length 19.93 s, peak 0.424103, RMS 0.032268. |
| Browser audio smoke test | AMI audio was observed unmuted and playing for 6.6 s; the media element reached `readyState` 4. |
| Flower Hub package | At the time of the hosted checks, `scripts/prepare_hub.py` built a local FAB from seven checked source files and reported zero configured-secret matches. Publication was completed later; see the current publication evidence above. |
| Recorded outputs | `recordings/verified-supergrid-kimi.mp4` and `recordings/verified-supergrid-ami.mp4` are stream-copy remuxes of actual recordings, retained locally. The AMI replay reached EOF before an AI camera decision, so that file is media/audio evidence, not proof of an AI cut on AMI. |

The verified Kimi role responses, Director choice, health recovery, and manual override are recorded in `.runtime/supergrid/kimi-acceptance.json`. The completed Flower run is also recorded in `.runtime/supergrid/run-history.json`. Do not expose provider keys or private account data from the local reports.

The separate AMI/OBS check is recorded in `.runtime/ami-acceptance.json`; its recording is `recordings/2026-09-29 12-24-34.mkv`. This confirms replay, five-feed health, EOF finalization, and the continuous audio path. It is not the Kimi AI recording.

A final 20.03-second AMI replay ran on the newly running coordinator `3067815238044157620`. Four AI reports arrived around 17–19 seconds, but no AI camera decision arrived before the short clip reached EOF. This confirms a latency limitation independent of coordinator renewal: the current complete directing round can exceed the 20-second excerpt. Use a longer clip for end-to-end AI demos; reduced latency remains an implementation improvement.

## Historical hosted critic and director flow

The coordinator fans out four camera jobs and one critic job in parallel. The critic receives the current measured round and bounded prior AI report history; it neither waits for nor consumes the four new camera reports. The Director receives the four camera replies, that round's critic response, and a fresh snapshot. In the Kimi proof, all six response IDs were accepted, and the Director selected a healthy camera. The controller remains responsible for current-round provenance, health, manual latch, and execution guards.

Kimi is the default model and the only profile verified on the current six-role topology. MiniMax inference was deliberately skipped for this update. Model-switch API and stale-response rejection pass automated tests; MiniMax inference and comparative editorial quality remain unverified on this topology.

## Reproduce local checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.runtime/pytest-check
.\.venv\Scripts\python.exe scripts\supergrid_agents.py status
.\.venv\Scripts\python.exe scripts\prepare_hub.py
```

`prepare_hub.py` is local packaging and secret review only; it does not publish. The seven-file package scan reported zero secret matches. The subsequently authorized v0.2.0 release was uploaded with `flwr app publish` from ignored `.runtime/hub-release-20260929/noesis-agents/`.

## Hackathon deliverables

The organizer's public requirements JSON was checked on 29 September 2026. It requires collaborative Flower Agents on SuperGrid, an AgentApp published to Flower Hub, a team registration form with a team description and GitHub repository, and a 3–5 minute demo. Individual tasks are limited to five minutes; the coordinator task is configured for 240 seconds. Endeavor is optional.

The historical six-node Kimi SuperGrid round is proven. The AgentApp is now published as `@thesid42/noesis-agents` v0.2.0. The team form, description/repository submission, and 3–5 minute demo remain pending.

## Remaining evaluation

- Rehearse the required 3–5 minute demo with real coordinator/node IDs and accepted response provenance.
- Measure repeated source-fault latency through the first usable encoded output frame.
- Evaluate editorial quality on a longer held-out AMI interval and measure fine A/V alignment.
- Do not claim MiniMax testing or a multi-machine SuperGrid result; neither is part of this verified evidence.
