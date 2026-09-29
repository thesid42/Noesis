# Flower hackathon requirements and Noesis evidence

Checked against the [Stanford 2026 organizer post](https://discuss.flower.ai/t/collaborative-agent-hackathon-stanford-ca-2026/1275) on 29 September 2026. The post was retrieved through its public Discourse JSON endpoint.

| Requirement | Noesis status |
|---|---|
| Collaborative Flower Agents running on SuperGrid | Historical hosted proof with Kimi: four camera agents, critic, and director exchange native Grid tasks and replies. Run `96528684490097715` completed successfully. |
| Tasks finish within five minutes of entering Running | Completed coordinator run took 245.545 seconds. Its application work budget is 240 seconds. Worker tasks are bounded separately. |
| Published Flower Hub AgentApp | **Not complete.** A seven-file package builds locally as `@thesid42/noesis-agents`; the user requested keeping it local. |
| Team name, members, emails, description, and GitHub submission | Public repository exists at [thesid42/Noesis](https://github.com/thesid42/Noesis). Submission through the [organizer form](https://flowerlabs.typeform.com/to/rQuplUGG) has not been verified. |
| 3–5 minute demo followed by questions | Test recordings exist locally; the final demo presentation remains to be prepared. |
| Endeavor integration | Optional bonus; not implemented. |

Local Flower orchestration is now the default for development and demos. It keeps real Flower inference and six AI roles, but a local run alone does not demonstrate the hosted SuperGrid requirement. Use the explicit hosted mode for new SuperGrid evidence; the completed run below remains historical evidence.

The judging criteria are use of Flower/SuperGrid, impact and originality, and demo/delivery. Kimi and MiniMax are supported model options in the event instructions. Kimi is the default and has been verified on the current six-agent implementation; MiniMax testing was explicitly skipped.

In hosted mode, model calls use Flower's injected inference runtime on the local SuperNodes. A loopback gateway supplies the matching private Nebius key for the selected model. The cloud coordinator contains no provider key and cannot access the local media controller directly; it reaches the studio through a Grid task sent to the director node.

The successful Kimi proof used synthetic footage. AMI playback and recorded audio are verified. The older hosted run did not finish an AI round before the 20-second clip ended; the new local Flower run did, with six accepted roles and one AI cut by EOF. This new AMI result is local-runtime evidence. Models currently receive measured source/audio features, not raw video, audio, or transcripts. See [validation evidence](VALIDATION.md) for the scope and timing limits.

Publication preparation follows [Flower's AgentApp publishing guide](https://flower.ai/docs/agent/how-to-guides/use-flower-hub.html). No Hub upload was performed.
