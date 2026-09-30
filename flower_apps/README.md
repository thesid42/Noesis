# Noesis SuperGrid AgentApp

Noesis is an autonomous production crew for multi-camera panel discussions. This Flower 1.39 AgentApp supplies four camera assessors, a critic, and a director. The full control room, media engine, and OBS bridge live in the [Noesis repository](https://github.com/thesid42/Noesis).

The default studio launcher uses one local Flower AgentApp with six continuous role loops and persistent inference connections to a local model gateway. Camera agents use Qwen3.5-9B through Flower AI with reasoning disabled; the critic and director use Nebius Kimi K2.7. The gateway holds provider credentials and routes requests by model ID. The browser and AgentApp prompts receive no provider keys.

For hosted SuperGrid, the Hub coordinator discovers six trusted SuperNode roles through Grid handshakes, obtains a leased round through the director SuperNode, fans camera and critic inference out in parallel, and asks the director to commit only when all five accepted report IDs are present. Hosted model calls use the Flower OpenAI-compatible runtime on SuperNodes. The Hub coordinator performs no direct HTTP access to the studio controller.

Each camera model receives one timestamp-pinned JPEG of its own view plus measured health, audio activity, quality, and timing. Images are bounded to 640x360 and 128 KiB. The director and critic receive structured camera assessments and available bounded editorial context; the local continuous mode includes optional recent transcripts and shot history. Full recordings, raw audio, and reference annotations stay local. Image and report timestamps, hashes, session epochs, and model identities are validated.

Current audio is primary speaker evidence; a still image assesses framing, an empty view, or directly visible board interaction. A clear face or static board is not sufficient reason to cut. The director makes editorial choices; deterministic code validates timing, source health, and manual override. Failed continuous requests release their matching lease and report errors without replacing the last accepted evidence. Already-aired targets remain rejected.

The Hub run prompt is a JSON object such as `{"mode":"coordinator","duration_s":240,"node_ids":["123","456"]}`. Omitted or unrecognized prompts default to coordinator mode. `node_ids`, when supplied by the trusted launcher, limits discovery to those Grid nodes. Each SuperNode must receive trusted node configuration with `role`, `agent_id`, `controller_url` (`http://127.0.0.1:8765`), and `camera_id` for camera roles.

App identity: `thesid42/noesis-agents`, version `0.2.0`. A hosted run requires the configured studio controller and trusted SuperNodes; publishing the AgentApp does not host the video dashboard or OBS. See the repository's [setup guide](https://github.com/thesid42/Noesis/blob/main/docs/FLOWER.md). No provider request is made during import or packaging.

Licensed under the MIT License; see [LICENSE](LICENSE).
