# Local validation — 28 September 2026

These are historical results from before the project was renamed to Noesis on 29 September 2026. Tests were not rerun for the rename. Referenced recordings, reports, and datasets are local artifacts and are excluded from the public repository.

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

Real-footage A/V alignment, headset identity/quality, speaker-following coverage, repeated-trial visible-output recovery latency, model-backed inference, and operation across multiple physical machines have not been established. The plan's numeric targets remain targets. The default application and the integration results above do not depend on provider credentials.

## Real AMI recording

`scripts/verify_ami.py` passed with the complete prepared ES2002a excerpt. All five cameras decoded as healthy, a Flower director proposal selected Closeup3, and the local baseline subsequently selected Closeup4. OBS produced a decodable recording of about 20 seconds and stopped automatically at the common input endpoint.

Recorded program audio is non-silent: peak 0.4242, RMS 0.03198, and about 20.07 seconds of extracted audio. The audio HTTP endpoint served a correct byte-range response. This proves that the persistent headset mix reached the OBS recording; it does not measure lip sync or certify the absence of every audio discontinuity.

The source excerpt has approximately 19.93 seconds common video duration and 20 seconds of audio. The manifest at `data/ami/ES2002a/prepared/clip-0-20/manifest.json` records mixed official source formats: Closeup1/Corner and audio use RM; Closeups 2–4 reuse AVI prefixes. All five prepared audio channels use the same fixed +18 dB gain, with no clipping (maximum source derivative peak 0.531). Full derivatives passed FFmpeg decode checks.

Official SMIL playback files assign all these camera views and the mix a `0s` start. A coarse comparison of the first Closeup1 AVI and RM frames also favored zero offset; neither is a fine A/V alignment measurement.

Evidence: `.runtime/ami-acceptance.json`, original `recordings/2026-09-28 21-55-09.mkv`, remuxed `recordings/verified-ami-demo.mp4`, and decoded frame `recordings/verified-ami-program.jpg`. The MP4 is a stream-copy remux, without re-encoding. The recording contains AMI attribution and an edited-replay label.
