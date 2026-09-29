# Noesis

**An autonomous production crew for panel discussions.** Noesis directs five synchronized views into one continuous program, with a shared replay clock and persistent program audio.

## How it works

Noesis runs six AI roles: four camera agents, a critic, and a director. A Flower SuperGrid coordinator fans out the four camera jobs and an independent critic job in parallel to six authenticated local SuperNodes. The critic reads the current measured round and bounded prior AI report history; it does not wait for or consume the four fresh camera reports. The Director combines all four current camera replies with the current critic result. Each coordinator task is limited to 240 seconds, below the event's five-minute task cap.

The models receive compact, measured camera and audio features—such as source health, speaking activity, energy, quality, and observation age. They do not receive raw frames, audio, or transcripts, so the demo makes no claims of semantic video or speech understanding. The four camera reports and critic assessment inform the AI director's shot choice.

The local controller protects playback and output: it checks the current source is healthy, rejects stale or mismatched AI results, enforces manual override and replay epochs, and executes accepted camera choices. AI is responsible for editorial choices; deterministic guards handle safety and output integrity. If AI work is unavailable, the controller holds a healthy current shot; source-health recovery can select another healthy view. It does not substitute a rules-based speaker-ranking director.

Kimi is the default and verified model profile. The MiniMax selector is available when configured but was not run or verified for this demo. Provider keys stay in local configuration and the model gateway, outside the browser and AgentApp prompts. The dashboard distinguishes configured models from models that have returned a verified response.

## Run the demo on Windows

Use Python 3.12 and `uv`. Configure Flower's authenticated SuperGrid connection before registering this project's six SuperNodes. Configure both Nebius profiles in `.env`; the current launcher loads both profiles for the in-session selector. See [Flower setup](docs/FLOWER.md).

```powershell
uv sync --python 3.12 --extra flower --extra dev
Copy-Item .env.example .env  # skip if .env already exists
# Fill the profile endpoint, model, and key values in the ignored .env.
.\.venv\Scripts\python.exe scripts\supergrid_agents.py setup
.\.venv\Scripts\python.exe scripts\run_demo.py --obs --model-profile kimi --duration-s 240
```

`setup` registers the six node identities with the authenticated `@thesid42/noesis` federation; run it once. `run_demo.py` starts the local controller, model gateway, six SuperNodes, and a bounded SuperGrid coordinator task. Keep the terminal open; Ctrl+C stops services owned by the launcher. Both profiles are required by the current launcher, with Kimi as the default. Keep the selector on Kimi for this demo; MiniMax was not tested.

Open **http://127.0.0.1:8765**, choose Synthetic or AMI input, and start a session. Synthetic feeds are visibly labeled and have no program audio. AMI replay uses the recorded headset mix; starting the session from the browser unlocks its audio playback. The `/program` page keeps the same mix across camera cuts and is the OBS browser source. `--obs` starts the configured portable OBS when needed and prepares its program scene. Omit `--obs` for preview output.

For local media preparation and OBS setup commands, see [Flower and demo setup](docs/FLOWER.md). There is no JavaScript build step.

## Hackathon delivery checklist

Requirements were checked against the organizer's public requirements JSON on 29 September 2026. Required deliverables include collaborative Flower Agents on SuperGrid, an AgentApp published to Flower Hub, a team registration form with a team description and GitHub repository, and a 3–5 minute demo. Keep each task at or below five minutes. Endeavor participation is optional.

All six authenticated federation nodes are online. Kimi was verified through coordinator run `96528684490097715`: all six AI roles returned accepted responses, and the Director selected Closeup3. The rehearsal passed manual override and recovered from a source fault in 984 ms. A later auto-renewal, run `3067815238044157620`, was still pending at the last check. The AgentApp package has been built and secret-checked locally; it has not been published to Flower Hub. The team form, final description, repository submission, and demo recording also remain submission tasks.

## Media and attribution

The AMI ES2002a adapter uses four participant close-ups, one room view, four isolated headset channels for measured speaker evidence, and the continuous headset mix for program audio. It is replay of recorded footage, not live capture. The latest smoke test decoded five healthy feeds and finalized at EOF; see [validation status](docs/VALIDATION.md) for duration and audio measurements. The longer 2–3 minute editorial evaluation and fine lip-sync measurement remain outstanding.

Source footage and audio: AMI Meeting Corpus, session ES2002a, AMI Project. [Corpus](https://groups.inf.ed.ac.uk/ami/corpus/) · [License](https://groups.inf.ed.ac.uk/ami/corpus/license.shtml) · [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Noesis modifications may include excerpting, transcoding, fixed audio gain, camera selection, overlays, and labeled test faults. No endorsement is implied. Carry this credit into exported demos.

## Project notes

- [Flower topology, configuration, and launch](docs/FLOWER.md)
- [Current verification and pending checks](docs/VALIDATION.md)
- [HTTP/API and state contract](BUILD_CONTRACT.md)
- [Product and evaluation plan](Noesis_Hackathon_Idea.md)
