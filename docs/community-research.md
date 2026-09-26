# Community research — 2026-09-26

Question: reusable Pi delegation from the existing Codex main conversation, with no Codex CLI or additional Codex agent; compact evidence, recoverable execution and project-owned acceptance.

| Source | Relevant capability | Gap for this workflow |
| --- | --- | --- |
| [howznguyen/pi-delegate-mcp](https://github.com/howznguyen/pi-delegate-mcp), inspected commit `8e258b772fb541c6c6bfc12ce949a3210da4cf5c` | MIT MCP server; Pi SDK delegates; model selection; spawn/run/steer/follow-up; read-only tools by default | `src/registry.ts` keeps workers in an in-memory Map; README recommends status polling; no project-specific durable acceptance receipt/worktree coordination equivalent to our existing runner. Its background spawn does not establish Codex task wake-up. |
| [pandysp/pi-mcp-server](https://github.com/pandysp/pi-mcp-server) | MIT MCP Pi/reply tools; session reuse, optional sandbox integration | Inspected package is 0.1.2 on Pi SDK `^0.52.9`; `src/session-store.ts` uses an in-memory map with eviction. Not a direct substitute for persistent project task/round recovery. |
| [minghinmatthewlam/pi-subagents](https://github.com/minghinmatthewlam/pi-subagents) | Pi RPC child agents, steer/wait, Pi-native completion notification | Its parent is Pi. A notification into a Pi parent is not evidence of notification into this existing Codex conversation. |

The installed-plugin directory search for “Pi coding agent” returned broader frameworks and unrelated services, not an exact matching Pi delegate plugin. This is a bounded search, not proof no other implementation exists. No community package was installed or executed for this investigation; the first two sources were cloned for reading under `/tmp/pi-collab-research-20260926`.

Decision after reviewing the local-only requirement: a skill plus direct Python-to-Pi CLI invocation is sufficient; MCP would add an unnecessary server and dependencies. Reuse our existing tested Python lifecycle, process-group termination and evidence helpers from the bake project; retain project constraints locally. Do not add a second model orchestration platform or copy a community status-polling workflow. No community source is copied merely for its README examples.

The original bake runner's Codex queue path and automatic Codex reviewer integrations are deliberately excluded. Without a verified native push/wake path, the plugin supports persistent background execution and explicit result collection, not unattended Codex continuation. Token savings require later comparable phase measurements.
