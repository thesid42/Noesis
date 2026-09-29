# Noesis SuperGrid AgentApp

This Flower 1.39 AgentApp supplies one Hub coordinator and six trusted SuperNode roles: four camera assessors, one measured-signal critic, and one director. The coordinator discovers role capabilities through Grid handshakes, requests each leased round from the local director SuperNode, fans camera and critic inference out in parallel, and asks the director to commit only when all five accepted report IDs are present.

All model calls run on SuperNodes through the Flower OpenAI-compatible runtime. The studio launcher points that runtime at the local Noesis model gateway; the gateway selects one of the configured Nebius model IDs and keeps provider credentials out of SuperLink, FAB configuration, prompts, and logs. The coordinator performs no direct HTTP access to the studio controller.

Camera and critic prompts receive only measured source health, speaker activity, signal quality, and timing. They do not receive raw media or transcripts, and the app does not claim semantic video or audio understanding. A round with missing, stale, invalid, expired, or rejected evidence is skipped without a fallback camera decision.

The Hub run prompt is a JSON object such as `{"mode":"coordinator","duration_s":240,"node_ids":["123","456"]}`. Omitted or unrecognized prompts default to coordinator mode. `node_ids`, when supplied by the trusted launcher, limits discovery to those Grid nodes. Each SuperNode must receive trusted node configuration with `role`, `agent_id`, `controller_url` (`http://127.0.0.1:8765`), and `camera_id` for camera roles.

Publish this directory as the `thesid42/noesis-agents` Flower app at version `0.2.0`. Local development dependencies are declared in `pyproject.toml`; no provider request is made by the app during import or packaging.

Licensed under the MIT License; see [LICENSE](LICENSE).
