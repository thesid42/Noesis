# Local validation

Referenced recordings, reports, and datasets are local artifacts and are excluded from the public repository.

## Nebius through Flower — 29 September 2026

Both profiles passed `scripts/verify_nebius.py`, Kimi first and MiniMax second, with five live Flower 1.39.0 AgentApps and OBS. Model calls used the injected Flower runtime Responses endpoint, which forwarded to the configured Nebius deployment. Four camera agents and the rules camera-proposal loop continued independently of the model worker.

| Check | Kimi K2.7 Code | MiniMax M3 |
| --- | --- | --- |
| Accepted model policies | 3 | 3 |
| Cuts applying a current model policy | 4 | 4 |
| End-to-end policy call latencies in final rehearsal | 3.531, 3.485, 3.500 seconds | 5.500, 4.000, 4.515 seconds |
| Black-fault request to controller recovery | 1,015 ms | 969 ms |
| Manual latch and policy invalidation | Passed | Passed |
| Decodable, nonblank OBS recording | 38.83 seconds | 39.23 seconds |

Each trial injected the black fault just after a fresh shot so an ordinary speaker transition could not mask the health recovery event. Both runs stopped recording successfully. Evidence includes safe model response IDs, token counts, Flower run IDs, accepted policy events, and executed camera cuts in `.runtime/nebius-kimi-acceptance.json` and `.runtime/nebius-minimax-acceptance.json`. Playable copies are `recordings/verified-kimi-demo.mp4` and `recordings/verified-minimax-demo.mp4`.

These are synthetic control-path checks, not a model-quality benchmark. Latency includes the Flower runtime and local overhead, and varies: an earlier cold Kimi policy took 22.672 seconds. Policies have a 30-second controller lease and 20-second lifetime after acceptance; stale replies are rejected while local camera control continues. The final runs had no editorial-policy timeouts. Each had one legacy fast rules-request timeout and two minimum-shot proposal rejections; these are separate from hosted inference failures.

The model selects only 4–8 second minimum shots and `hold`/`wide` overlap behavior. Executing its policy does not establish improved editorial quality, visual understanding, lip sync, or repeated-trial output recovery. Only compact camera observations and event types reach Nebius. Keys stay in ignored configuration and the SuperLink environment.

The updated regression suite has **40 passing tests**, including policy expiration, stale session/override rejection, malformed policy rejection, health precedence, and checking a newly accepted policy again before committing a cut.

Flower 1.39.0 also attempts automatic conversation titles with its hardcoded `openai/gpt-5-nano` model. The supplied deployments return nonfatal 404s for those metadata calls. They are separate from the successful Kimi/MiniMax inference calls and are not counted as director results.

## Original rules and AMI validation — 28 September 2026

The following results predate the rename to Noesis. They are retained as historical evidence; the Nebius rehearsal above is the newer integration check.

## Automated regression suite

30 tests passed. Coverage includes generated AVI/WAV decoding and causal VAD, signal-derived black/freeze/offline detection, stale and duplicate decisions, manual override, transition races, OBS readback, EOF finalization, pause timing, and retrying a recording stop after connection loss.

## Actual running integration

`scripts/verify_demo.py` passed against five separate Flower 1.39.0 AgentApp runs, the local HTTP broker, and portable OBS 32.2.2.

| Check | Observed result |
| --- | --- |
| Input | Explicitly labeled synthetic test feeds |
| Director | Rules mode; no model calls or credentials |
| Accepted Flower director camera cuts | 1 |
| Black-frame injection to controller fallback selection | 1,016 ms, single trial |
| Manual latch across a speaker change | Passed |
| Pause freezes replay clock | Passed |
| Actual OBS recording | 25.57 seconds, decodable nonblank frames and distinct shots |
| Replay EOF | Session and recording stopped; epoch invalidated |

The recording is `recordings/2026-09-28 21-37-03.mkv`; a decoded frame is `recordings/verified-program.jpg`. Machine-readable events, run IDs, and metrics are in `.runtime/acceptance.json`.

`scripts/verify_resilience.py` also passed: after all five managed Flower runs stopped and their heartbeats expired, the local baseline continued making cuts. The runs were restored. An independent OBS WebSocket client then stopped a recording, and the controller's background status poll detected it. Details are in `.runtime/resilience.json`.

## Still to validate

The installed 20-second prepared set is only an adapter/recording smoke test, not the planned 2–3 minute editorial evaluation. Downloaded prefixes are not treated as full originals.

Real-footage A/V alignment, headset identity/quality, speaker-following coverage, repeated-trial visible-output recovery latency, model editorial-quality improvement, and operation across multiple physical machines have not been established. The plan's numeric targets remain targets. The default application and the original rules integration results do not depend on provider credentials.

## Real AMI recording

`scripts/verify_ami.py` passed with the complete prepared ES2002a excerpt. All five cameras decoded as healthy, a Flower director proposal selected Closeup3, and the local baseline subsequently selected Closeup4. OBS produced a decodable recording of about 20 seconds and stopped automatically at the common input endpoint.

Recorded program audio is non-silent: peak 0.4242, RMS 0.03198, and about 20.07 seconds of extracted audio. The audio HTTP endpoint served a correct byte-range response. This proves that the persistent headset mix reached the OBS recording; it does not measure lip sync or certify the absence of every audio discontinuity.

The source excerpt has approximately 19.93 seconds common video duration and 20 seconds of audio. The manifest at `data/ami/ES2002a/prepared/clip-0-20/manifest.json` records mixed official source formats: Closeup1/Corner and audio use RM; Closeups 2–4 reuse AVI prefixes. All five prepared audio channels use the same fixed +18 dB gain, with no clipping (maximum source derivative peak 0.531). Full derivatives passed FFmpeg decode checks.

Official SMIL playback files assign all these camera views and the mix a `0s` start. A coarse comparison of the first Closeup1 AVI and RM frames also favored zero offset; neither is a fine A/V alignment measurement.

Evidence: `.runtime/ami-acceptance.json`, original `recordings/2026-09-28 21-55-09.mkv`, remuxed `recordings/verified-ami-demo.mp4`, and decoded frame `recordings/verified-ami-program.jpg`. The MP4 is a stream-copy remux, without re-encoding. The recording contains AMI attribution and an edited-replay label.
