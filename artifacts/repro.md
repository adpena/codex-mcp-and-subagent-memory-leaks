# Subagent/MCP Concurrency Investigation

Date: 2026-04-12

## Scope

This document records the current upstream behavior observed while debugging high-concurrency Codex failures involving:

- many simultaneous Codex sessions
- recursive subagent spawning
- stdio MCP servers
- long-lived or hanging MCP tool calls
- terminal/TUI degradation over time

The goal is full reproducibility without depending on memory of the debugging session.

## Reported External Symptoms

Observed by the user in real usage on macOS with large memory headroom:

- degradation accumulates over time under heavy concurrency
- the session with the most subagents is usually the first to fail
- rendering quality degrades before the final crash
  - flickering
  - jank
  - improper rendering
  - later `memallocstack` failures
- after the final failure, `Ctrl+C` may stop working in the affected terminal pane
- once one session becomes contaminated, recovery attempts can spread damage across agents
- agents may start killing each other’s processes while trying to recover

## Current Upstream Shape

Two upstream properties matter most:

1. Spawned agents reserve a slot in the shared agent registry and historically only released it on explicit shutdown/close or hard death.

   Relevant code:
   - [codex-rs/core/src/agent/registry.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/agent/registry.rs)
   - [codex-rs/core/src/agent/control.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/agent/control.rs)

2. Each session owns its own `SessionServices`, including its own `McpConnectionManager`.

   Relevant code:
   - [codex-rs/core/src/state/service.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/state/service.rs)
   - [codex-rs/core/src/codex.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/codex.rs)

That means recursive subagent spawning can multiply MCP child processes quickly.

## Reproduction Harnesses Added

### 1. Hostile soak driver

Script:
- [scripts/soak_codex_concurrency.py](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/scripts/soak_codex_concurrency.py)

Purpose:
- launch many `codex exec` sessions concurrently
- assign hostile fixture scenarios
- sample process state over time
- summarize surviving workers/processes and lifecycle counters

Toolchain dependencies:
- built `codex` binary
- built `test_stdio_server` binary
- `ps`, `kill`
- Python 3

Build commands:

```bash
cd codex-rs
cargo build -p codex-cli --bin codex
cargo build -p codex-rmcp-client --bin test_stdio_server
```

Example run:

```bash
./scripts/soak_codex_concurrency.py \
  --workers 1 \
  --duration-sec 6 \
  --spawn-interval-ms 200 \
  --sample-interval-sec 2 \
  --debug-lifecycle \
  --scenarios spawn_recursive
```

### 2. Hanging stdio MCP tool

Test server:
- [codex-rs/rmcp-client/src/bin/test_stdio_server.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/rmcp-client/src/bin/test_stdio_server.rs)

Added behavior:
- `hang` tool never returns
- optional `MCP_TEST_PID_FILE` env var appends server PID for process tracking

### 3. Focused repro tests

- [codex-rs/rmcp-client/tests/stdio_timeout_recovery.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/rmcp-client/tests/stdio_timeout_recovery.rs)
  - verifies isolated stdio timeout behavior at the `rmcp-client` layer

- [codex-rs/app-server/tests/suite/v2/mcp_shutdown.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/app-server/tests/suite/v2/mcp_shutdown.rs)
  - verifies app-server shutdown behavior with a hanging stdio MCP call

- [codex-rs/tui/tests/suite/mcp_hang_interrupt.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/tui/tests/suite/mcp_hang_interrupt.rs)
  - PTY-oriented terminal-surface repro for `codex exec` plus hanging MCP

## Observed Baselines Before the Fix

### Recursive spawn soak

Run output:
- [summary.json](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/.tmp/codex-soak-t8ajzcj7/summary.json)

Key observation:

- one `spawn_recursive` worker produced:
  - `agent_slot_reserved = 6`
  - `agent_slot_released = 0`
  - `mcp_spawned = 7`
  - `mcp_dropped = 7`

Process count sampled by the soak:

- began at `1`
- rose to `8`

Interpretation:

- recursive subagent activity can multiply stdio MCP processes rapidly
- live agent slots were not being released during the run
- cleanup did eventually happen at teardown, but only after pressure had already built up

### Mixed soak

Run output:
- [summary.json](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/.tmp/codex-soak-4xp1gofy/summary.json)
- [summary.json](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/.tmp/codex-soak-5pgdag12/summary.json)

Key observation:

- a `spawn_then_hang` worker reserved a spawned-agent slot and spawned 2 MCP stdio children
- a `hang_only` worker spawned 1 MCP stdio child
- total tracked processes climbed well above the worker count

## Important Negative Findings

These are things that looked suspicious but did not fully explain the failure:

1. Isolated `rmcp-client` timeout handling is not the whole bug.

   `stdio_timeout_recovers_for_subsequent_calls` passed:

   - a timed-out stdio MCP tool call can recover cleanly in isolation

2. App-server can shut down promptly if stdin is explicitly closed.

   `dropping_client_with_hanging_stdio_mcp_call_exits_promptly` showed that once the shutdown path is actually reached, the app-server does not inherently require a long stall.

3. The terminal-surface failure is still plausible, but the PTY repro is not yet the final authority on macOS pane corruption.

   The current PTY repro exercises `codex exec` under a PTY and is useful, but it is still an approximation of the user’s real multi-pane, multi-session environment.

## Crux

The dominant accumulation path is not just “completed agents linger.”

The stronger failure is:

1. a root session spawns many live descendants
2. each descendant can eagerly own its own MCP stdio process set
3. when the root session is interrupted or shuts down, descendant cleanup was previously only partial
4. under recursive/high-concurrency fanout, some descendants escaped the first shutdown sweep
5. the busiest session therefore kept the most live MCP child processes and agent slots the longest

This is why the most-subagent session tends to be the first one to become unstable.

## Current Fix Direction

Implemented so far:

1. Finalized (`Completed` / `Errored`) spawned agents on the stable multi-agent path are retired from
   live resource ownership while preserving their final status for collaboration UX.

2. Root-session shutdown now performs repeated descendant shutdown passes instead of a single
   one-shot descendant snapshot.

Touched code:
- [codex-rs/core/src/agent/registry.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/agent/registry.rs)
- [codex-rs/core/src/agent/control.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/agent/control.rs)
- [codex-rs/core/src/codex.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/codex.rs)
- [codex-rs/core/src/tools/handlers/multi_agents/wait.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/tools/handlers/multi_agents/wait.rs)
- [codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs](/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex/codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs)

Intent:

- `Completed` and `Errored` spawned agents should stop consuming live slots and live thread/session resources
- explicitly closed agents should still become `NotFound`
- `list_agents` and `wait_agent` should still surface meaningful final statuses
- root-session shutdown should drain as much of the live subagent tree as possible before returning

## Lifecycle Logging Toggles

For debugging only:

- `CODEX_DEBUG_AGENT_LIFECYCLE=1`
- `CODEX_DEBUG_MCP_LIFECYCLE=1`
- `CODEX_DEBUG_THREAD_LISTENERS=1`

The soak script sets these automatically when `--debug-lifecycle` is used.

## Current Hypothesis

The current best explanation is:

1. recursive/high-concurrency subagent spawning multiplies live sessions
2. each live session can eagerly own its own MCP stdio process set
3. root-session shutdown historically did not fully drain the live descendant tree under concurrent fanout
4. finalized descendants were also not reclaimed aggressively enough on the stable path
5. process/task/listener pressure therefore builds in the busiest root session first
6. once the terminal-facing session destabilizes, the final hard failure can bypass or defeat clean terminal recovery

Short form:

`live descendant shutdown leak + finalized-agent retention + per-session MCP fanout`

## Remaining Open Questions

- How much of the user-visible terminal corruption is caused by backend accumulation vs. TUI/terminal restore behavior after the final crash?
- Is eager per-session MCP startup itself too expensive for spawned agents, even with better final-state retirement?
- Do we need the same retirement model on the `multi_agent_v2` path, which currently uses different completion plumbing?

## Commands Used Frequently

Targeted tests:

```bash
cd codex-rs
cargo test -p codex-rmcp-client stdio_timeout_recovers_for_subsequent_calls -- --nocapture
cargo test -p codex-app-server dropping_client_with_hanging_stdio_mcp_call_exits_promptly -- --nocapture
cargo test -p codex-tui ctrl_c_exits_codex_exec_during_hanging_stdio_mcp_tool_call -- --nocapture
cargo test -p codex-core spawn_agent_releases_slot_after_completion -- --nocapture
cargo test -p codex-core wait_agent_returns_final_status_without_timeout -- --nocapture
cargo test -p codex-core multi_agent_v2_list_agents_returns_completed_status_and_last_task_message -- --nocapture
cargo test -p codex-core resume_agent_restores_closed_agent_and_accepts_send_input -- --nocapture
```

Broad verification:

```bash
cd codex-rs
cargo test -p codex-core
```

Formatting:

```bash
cd codex-rs
cargo fmt --all
```

## Notes

- `just fmt` was not available in this environment, so `cargo fmt --all` was used instead.
- Some existing integration tests can hit live OpenAI endpoints during the full `codex-core` matrix; this is expected in the current test environment.
