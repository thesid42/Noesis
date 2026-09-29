# Inference latency investigation

## Continuous camera updates, perception, and delayed output

Measured on 29 September 2026 in local Flower run `6214313413897169354`, with Qwen3.5-9B camera agents through Flower (`reasoning.effort=none`), Kimi critic/director through Nebius, local speech/frame analysis enabled, OBS recording, and a five-second program delay.

| Measurement | Observed |
|---|---:|
| Accepted Qwen camera reports | 23 |
| Camera latency, median / range | 1.891 s / 1.188–6.719 s |
| Accepted Kimi critic reports | 5 |
| Accepted Kimi director decisions | 3 |
| Director latency, median / range | 2.469 s / 1.282–3.266 s |
| Scheduled / actually aired AI cuts | 1 / 1 (other decisions held the shot) |
| First aired AI cut after replay start | about 10.7 s |
| Missed output deadlines | 0 |
| Capture rate across five sources, median | 18.06 fps |
| Program MJPEG delivery rate | 25.35 updates/s |
| Peak JPEG buffer memory | 12.06 MiB |
| State API latency, p95 | 26.9 ms |

This is one short AMI excerpt, not a reliable tail-latency or editorial-quality evaluation. A preceding all-Kimi run had one decision miss the five-second window. The delay budgets time after the director's fixed target snapshot; it does not remove initial camera/critic bootstrap or make old reports current. Choose eight or ten seconds in the UI if the current provider workload repeatedly exceeds five seconds. A late choice is rejected; it never rewrites footage that has already aired.

The source files themselves have different rates: Closeup1 and Corner are 12.54 fps, and the other three close-ups are 25 fps, averaging about 20 fps. A 30 fps capture/stream target does not create new motion in those sources. In a separate 12-second local media check, enabling ASR/CV left median capture throughput essentially unchanged at 19.10 fps; snapshot p95 was 0.50 ms without and 0.66 ms with perception. Under the complete live workload, four-second speech chunks had a sampled median processing time of 1.86 seconds; five-camera visual batches had a sampled median of 63 ms. Both run in background workers and never gate a director request or frame delivery. These workload samples do not isolate CPU cost precisely.

The OBS recording is 25.166 seconds including initial buffer fill, with silence in its first four seconds. Waveform correlation against the source mix measured about 5.35 seconds of audio offset, including startup/output capture timing; three windows ranged from 5.32 to 5.38 seconds. This establishes delayed continuous audio, not frame-accurate lip-sync. The browser source must be refreshed after player changes: setup now refreshes only Noesis's existing source and refuses to change it while recording.

Private evidence: `.runtime/continuous-ami-live.json`, `.runtime/continuous-media-benchmark.json`; local remux: `recordings/verified-qwen-delayed-ami.mp4`. The previous all-Kimi run is preserved separately as `.runtime/continuous-ami-kimi-only.json`.

Qwen3.5-4B was not present in the public OpenRouter catalog and Flower rejected that model ID. The user approved Qwen3.5-9B instead. A `low` reasoning probe consumed all 320 output tokens without camera JSON; an explicit `none` probe completed with 55 tokens, so non-thinking mode was approved and enabled. Four camera roles use that setting; Kimi requests do not receive the Qwen reasoning override.

## Persistent gateway comparison

On 29 September 2026, three fresh synthetic Kimi rounds were measured with each transport: 18 accepted responses per run, all six roles, with no unmatched responses, model timeouts, or rejected proposals. The baseline native local Flower run was `1073690520930975656`; the persistent gateway run was `5195545927455980589`.

| Measurement | Flower model tasks | Persistent gateway |
|---|---:|---:|
| Median camera request | 9.703 s | 4.102 s |
| Median critic request | 5.515 s | 2.360 s |
| Median director request | 4.172 s | 0.688 s |
| Median director Nebius round trip | 0.687 s | 0.688 s |
| Median camera local request overhead | 4.437 s | 0.047 s |
| Median director local request overhead | 3.485 s | below clock resolution |
| First decision after replay start | 12.360 s | 6.172 s |
| Subsequent decision intervals | 17.828, 15.906 s | 6.969, 7.000 s |

Director requests were about **84% faster** in this sample. The nearly identical director provider timings and much smaller local residual indicate that the removed local request path accounted for most of its previous delay. This change combines persistent client reuse with bypassing Flower's per-request MODEL subprocess; it does not isolate those two effects from one another. The native local Flower AgentApp still hosts and coordinates all six AI roles. These are direct gateway model calls, not Flower MODEL tasks or a hosted SuperGrid run.

The persistent client is initialized once per AgentApp crew and shares a six-connection HTTP pool across rounds. Its five first-round assessment calls had 1.063–1.251 s of residual startup overhead; subsequent assessment residuals were 0–48 ms. A zero residual is below the Windows clock's resolution, not proof of zero work. The first-decision measurement starts with an already connected crew and excludes overall application startup. The gateway's upstream connection pool already existed before this change.

These sequential runs use the same Kimi model, role instructions, strict output schemas, 320-token cap, synthetic scenario, and five-assessments-then-director schedule. They do not use byte-identical prompts: observations, generated reasons, and shot choices vary. Mean director input/output tokens were 1740/66.7 for Flower and 1702/69.3 for the gateway. Provider load may also vary. Three rounds support this smoke-test conclusion, not a reliable tail-latency estimate. Nebius timings include network, provider queueing, and inference, not compute alone. Per-component medians need not add to the median total.

The first gateway attempt could not connect upstream from a network-restricted process and yielded no accepted model responses. It was stopped and excluded; the successful run above used a network-enabled launcher. Private results are retained in `.runtime/local/latency-benchmark-flower-fresh.json`, `latency-benchmark-persistent-gateway.json`, and `persistent-comparison.json`.

Persistent gateway connections are now the local default. To repeat the benchmark:

```powershell
.\.venv\Scripts\python.exe scripts\run_demo.py --obs --inference-transport gateway --trace-inference
.\.venv\Scripts\python.exe scripts\benchmark_latency.py --rounds 3 --label persistent-gateway
```

The second command runs from another terminal with no replay active. To compare the original path, restart with `--inference-transport flower` and use `--label flower-fresh`. Local launchers default to `gateway`; hosted SuperGrid defaults to `flower`. API state and run metadata explicitly identify the selected transport. The benchmark stops its synthetic replay after completion; the app remains available. No MiniMax or Qwen inference was used.

Each request retains a cancellable wall-time deadline and all existing schema, epoch, provenance, manual-override, and source-health validation. Timeout cancellation closes the client request; it does not guarantee cancellation of work already running at Nebius.

The final regression suite passes **68 tests**, including shared-client reuse, successful requests after a canceled timeout, orderly cancellation of pending requests before shutdown, credential routing, runtime-specific defaults, and unchanged AI acceptance guards.

## Earlier separation measurement

Measured on 29 September 2026 using three complete synthetic Kimi rounds (18 accepted model responses) in native local Flower run `1073690520930975656`. No MiniMax calls or Qwen deployment were made.

## Observed separation

Each agent already records the time around its complete Flower-runtime request. The gateway now optionally records provider response ID and elapsed timings. Joining on the exact response ID separates the Nebius round trip from local gateway processing and the remaining request path.

| Role | Calls | Mean complete request | Mean Nebius round trip | Mean gateway processing | Mean Flower/SDK/local transport residual |
|---|---:|---:|---:|---:|---:|
| Camera | 12 | 8.604 s | 3.629 s | 0.001 s | 4.974 s |
| Critic | 3 | 7.255 s | 2.073 s | below clock resolution | 5.182 s |
| Director | 3 | 4.740 s | 1.568 s | below clock resolution | 3.172 s |

These are per-call arithmetic means and add up by construction; parallel camera times must not be summed to estimate a round. Gateway processing ranged from 0 to 15 ms at the host clock's resolution. The first director response arrived 19.297 seconds after replay start; the subsequent completed rounds were 13.031 and 14.625 seconds apart. Earlier tests were faster, so the original 11.5-second observation was not a latency guarantee.

Nebius round-trip timing includes network travel, provider queueing, and generation. It is not a measurement of GPU compute alone. The residual includes SDK/client setup, Flower's task dispatch and per-call model subprocess, reply polling, and loopback transport. It must not be described as entirely Flower CPU time. A separate no-network experiment constructing and closing 15 AsyncOpenAI clients in batches of five measured a 418 ms median; that experiment is contextual evidence, not an exact decomposition of each production request.

The gateway itself is not the bottleneck. Both the upstream model call and the runtime request path matter. A faster camera model alone will leave several seconds of current overhead.

## Reproduce

Start local Noesis with tracing:

```powershell
.\.venv\Scripts\python.exe scripts\run_demo.py --obs --trace-inference --inference-transport flower
```

With no replay active, run:

```powershell
.\.venv\Scripts\python.exe scripts\benchmark_latency.py --rounds 3
```

This uses live Kimi calls and a synthetic preview, then stops its replay. Private timing records and the joined report are stored in `.runtime/local/gateway-timings.jsonl` and `.runtime/local/latency-benchmark.json`. Logs contain timings, response IDs, model identity, and a request hash; no prompts, responses, keys, headers, or provider URLs. Tracing is opt-in and restricted to `.runtime`. Unit tests cover timing correlation and sanitized logs; the full suite passes 55 tests.

## Recommended next changes

1. Persistent gateway connections are implemented, measured above, and the default locally. The original Flower model-task route remains available for comparison and is used by hosted SuperGrid.
2. Qwen3.5-9B camera roles through Flower are now enabled and measured above. A separately hosted `Qwen/Qwen3.5-4B` endpoint remains a possible future comparison; no 4B deployment has been measured here.
3. Independent assessment leases and a separate director lease are now implemented locally, measured above. Hosted coordinator rounds remain available. Each role samples a new snapshot after its prior call finishes, with at most one call per role. The director pins immutable copies of five sufficiently recent reports plus a fresh source snapshot. New reports can arrive while it thinks without replacing the evidence being validated.

Continuous mode enforces acquisition-age limits, role-specific model assignment, and invalidation on pause, seek, manual override, or model changes. Director validation must use its pinned response IDs and current source health. It must never reuse old evidence just by resetting its timestamp on arrival. Continuous updates improve steady-state cadence; first startup still waits for initial reports. Timing and quality should be remeasured with four concurrent Qwen requests before choosing final freshness limits.
