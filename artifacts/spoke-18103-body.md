FYI: I posted a source-level analysis on
[#17832](https://github.com/openai/codex/issues/17832) that may be relevant
to the resource-pressure pattern here. The `codex-aarch64-apple-darwin`
processes accumulating before the watchdog panic line up with a
subagent-retention + root-shutdown gap in `codex-rs/core/src/agent/` —
finalized subagents don't release their registry slot, so their session
(and its `McpConnectionManager` + stdio MCP child processes) survives past
the point where it should have entered shutdown.

The terminal-emulator side of what you observed (zellij / Ghostty
specifics) is separate from the backend leak path, but the backend
accumulation is what produces the underlying memory pressure.
[#19753](https://github.com/openai/codex/pull/19753) (merged 2026-04-28)
closes part of the surface; the rest of the analysis and a suggested fix
outline are at https://github.com/adpena/codex-mcp-and-subagent-memory-leaks.

Hope it's useful.
