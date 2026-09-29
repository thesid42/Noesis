# Validation status

Evidence updated 29 September 2026. Generated reports, recordings, keys, and run manifests stay in ignored local paths.

## Current checks

| Check | Observed result |
|---|---|
| Regression suite | **42 passed**. |
| Authenticated federation | All six registered Noesis SuperNodes are online in `@thesid42/noesis`. |
| Kimi SuperGrid inference | **Passed.** Cloud coordinator run `96528684490097715` finished `completed` in 245.545 s and produced accepted responses from all six AI roles; the Director selected Closeup3. The run stayed within the five-minute event limit. Safe role provenance is in `.runtime/supergrid/kimi-acceptance.json` and `.runtime/supergrid/run-history.json`. |
| Kimi round timing | The first 30-second round expired. A later round completed in about 23 seconds; individual Kimi model calls took 4.3–7.0 seconds. |
| Health and manual guards | The Kimi rehearsal recovered from a source fault in 984 ms and passed the manual override check. |
| AMI/OBS output smoke test | Five healthy feeds; video finalized at EOF. Recording length 20.03 s; audio length 19.93 s, peak 0.424103, RMS 0.032268. |
| Browser audio smoke test | AMI audio was observed unmuted and playing for 6.6 s; the media element reached `readyState` 4. |
| Flower Hub package | `scripts/prepare_hub.py` built a local FAB from seven checked source files and reported zero configured-secret matches. It was not published and remains in ignored `.runtime/hub/`. |
| Recorded outputs | `recordings/verified-supergrid-kimi.mp4` and `recordings/verified-supergrid-ami.mp4` are stream-copy remuxes of actual recordings, retained locally. The AMI replay reached EOF before an AI camera decision, so that file is media/audio evidence, not proof of an AI cut on AMI. |

The verified Kimi role responses, Director choice, health recovery, and manual override are recorded in `.runtime/supergrid/kimi-acceptance.json`. The completed Flower run is also recorded in `.runtime/supergrid/run-history.json`. Do not expose provider keys or private account data from the local reports.

The separate AMI/OBS check is recorded in `.runtime/ami-acceptance.json`; its recording is `recordings/2026-09-29 12-24-34.mkv`. This confirms replay, five-feed health, EOF finalization, and the continuous audio path. It is not the Kimi AI recording.

A final 20.03-second AMI replay ran on the newly running coordinator `3067815238044157620`. Four AI reports arrived around 17–19 seconds, but no AI camera decision arrived before the short clip reached EOF. This confirms a latency limitation independent of coordinator renewal: the current complete directing round can exceed the 20-second excerpt. Use a longer clip for end-to-end AI demos; reduced latency remains an implementation improvement.

## Critic and director flow

The coordinator fans out four camera jobs and one critic job in parallel. The critic receives the current measured round and bounded prior AI report history; it neither waits for nor consumes the four new camera reports. The Director receives the four camera replies, that round's critic response, and a fresh snapshot. In the Kimi proof, all six response IDs were accepted, and the Director selected a healthy camera. The controller remains responsible for current-round provenance, health, manual latch, and execution guards.

Kimi is the default model and the only profile verified on the current six-role topology. MiniMax inference was deliberately skipped for this update. Model-switch API and stale-response rejection pass automated tests; MiniMax inference and comparative editorial quality remain unverified on this topology.

## Reproduce local checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp=.runtime/pytest-check
.\.venv\Scripts\python.exe scripts\supergrid_agents.py status
.\.venv\Scripts\python.exe scripts\prepare_hub.py
```

`prepare_hub.py` is local packaging and secret review only. Keep the FAB, staging directory, hashes, and report in the workspace; **do not publish to Flower Hub**. The seven-file package scan reported zero secret matches.

## Hackathon deliverables

The organizer's public requirements JSON was checked on 29 September 2026. It requires collaborative Flower Agents on SuperGrid, an AgentApp published to Flower Hub, a team registration form with a team description and GitHub repository, and a 3–5 minute demo. Individual tasks are limited to five minutes; the coordinator task is configured for 240 seconds. Endeavor is optional.

The six nodes are online and a real Kimi SuperGrid round is proven. The AgentApp is prepared locally but not published, as requested. The team form, description/repository submission, and 3–5 minute demo remain pending.

## Remaining evaluation

- Capture AI Director cuts in an AMI recording after allowing a round to complete before EOF.
- Verify the selected camera and continuous audio together in that recording.
- Rehearse the required 3–5 minute demo with real coordinator/node IDs and accepted response provenance.
- Measure repeated source-fault latency through the first usable encoded output frame.
- Evaluate editorial quality on a longer held-out AMI interval and measure fine A/V alignment.
- Do not publish the local AgentApp package to Flower Hub unless the user explicitly requests that separate action.
- Do not claim MiniMax testing or a multi-machine SuperGrid result; neither is part of this verified evidence.
