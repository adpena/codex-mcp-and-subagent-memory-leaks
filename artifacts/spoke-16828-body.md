FYI: this looks like the same root cause as the long-session memory growth
documented in [#17832](https://github.com/openai/codex/issues/17832). I
posted a source-level analysis there that may be relevant — the defects sit
in platform-agnostic Rust code in `codex-rs/core/src/agent/{registry,
control}.rs` and `session/handlers.rs` (no `cfg(target_os)` guards), so they
apply to Linux as well as macOS / Windows.

Headline mechanism: under recursive subagent fanout, finalized
(`Completed` / `Errored`) subagents don't release their registry slot, so
their session — and its `McpConnectionManager` — stays alive past the point
where it should have entered shutdown. [#19753](https://github.com/openai/codex/pull/19753)
(merged 2026-04-28) closes the leak surface for sessions that *do* enter
shutdown, but doesn't address the retention path. Full analysis, suggested
fix outline, and candidate patch: https://github.com/adpena/codex-mcp-and-subagent-memory-leaks.

Hope it's useful context.
