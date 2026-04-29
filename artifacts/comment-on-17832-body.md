[#19753](https://github.com/openai/codex/pull/19753) (merged 2026-04-28) tears down `McpConnectionManager` for any session that enters shutdown. Two structural gaps in `codex-rs/core` remain on current `main` ([`80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)):

1. `AgentRegistry::release_spawned_thread` ([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99)) is called only from `agent/control.rs:693` (`CodexErr::InternalAgentDied`) and `agent/control.rs:714` (`shutdown_live_agent`). `Completed` / `Errored` finalization does not retire a slot, so a finalized subagent's session stays alive holding its `McpConnectionManager`. V1 has a completion watcher gated `!Feature::MultiAgentV2` at `control.rs:307`; V2 has no equivalent.
2. `session::handlers::shutdown` ([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879)) does not invoke `live_thread_spawn_descendants` ([`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164), already used by `close_agent`).

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# 0 matches
git grep -n 'live_thread_spawn_descendants' codex-rs/core/src/session/
# 0 matches
```

Cross-platform reports: [#16828](https://github.com/openai/codex/issues/16828) (Linux: 49.4 GB peak, hard-froze a CachyOS workstation), [#12414](https://github.com/openai/codex/issues/12414) (Windows: 90 GB commit growth → OOM), [#19381](https://github.com/openai/codex/issues/19381) (Windows app + VSCode: 10 GB+), [#18103](https://github.com/openai/codex/issues/18103) (macOS + Ghostty/zellij). The defects are platform-agnostic Rust code (no `cfg(target_os)` guards on the affected files).

## Suggested fix

Topic branch [`fix/subagent-retention-after-19753`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/tree/main/codex), patch [`pr1-subagent-retention-after-19753.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-after-19753.patch) (1502 lines, applies cleanly on top of `80fb0704ee`):

- `AgentMetadata.slot_state: SlotState` (`NotTracked` / `Active` / `Retired { last_status }`), replacing the prior `slot_active: bool` + `last_status: Option<AgentStatus>` so the lifecycle is explicit in the type system.
- `AgentRegistry::retire_spawned_thread(id, status)` transitions `Active → Retired`, decrements `total_count` once. Idempotent under repeated retire / interleaved release.
- `AgentControl::retire_finalized_agent` flushes rollout, enqueues `Op::Shutdown` on the session's submission channel (so the session loop drains it into `handlers::shutdown`, picking up #19753's `begin_shutdown()`), removes the live thread, and retires the registry slot. Invoked from V1's `maybe_start_completion_watcher` (existing) and from V2's `Session::maybe_notify_parent_of_terminal_turn` via `tokio::spawn` — synchronous V2 invocation self-deadlocks because the call site runs inside `Session::send_event` and the bounded submission channel's receiver is the same loop. The completion watcher and V2 path retire only `Completed` / `Errored`. `Shutdown` is intentionally not retired (regressed an existing resume-path test).
- `get_status` / `wait_agent` / `list_agents` fall back to cached registry status when no live thread exists. `resume_agent` switches to `has_live_thread()` instead of treating any non-`NotFound` status as proof of non-resumability.
- `session::handlers::shutdown` calls `live_thread_spawn_descendants` with a hard deadline-bounded sweep: 30 s wall-clock + 64-sweep cap, with `tokio::time::timeout_at` wrapping each `shutdown_live_agent` call so a stuck descendant cannot block past the deadline, and a 50 ms `sleep_until` between sweeps whose live-set didn't shrink.
- `SpawnReservation::Drop` releases the reserved nickname when spawn setup fails before commit.

## Verification

Fresh clone of `openai/codex@80fb0704ee` with the patch applied:

```
$ git apply --check patches/pr1-subagent-retention-after-19753.patch
$ cargo fmt --all -- --check
$ cargo clippy --workspace --tests -- -D warnings
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 25.20s

$ cargo test -p codex-core --lib retire_releases_slot_and_preserves_cached_status
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 1651 filtered out; finished in 0.57s

$ cargo test -p codex-core --lib v2_spawn_agent_releases_slot_after_completion
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.42s

$ cargo test -p codex-core --lib root_shutdown_shuts_down_live_spawned_descendants
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.29s

$ cargo test -p codex-core --lib failed_spawn_releases_reserved_nickname
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib retire_after_release_does_not_double_decrement
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib retire_is_idempotent_for_repeated_calls
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib release_after_retire_does_not_double_decrement
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 0.00s
```

### Failing-then-passing demonstration

Same fresh clone. Comment out the V1 retirement call in `agent/control.rs::maybe_start_completion_watcher` (lines 1110-1116):

```
$ cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion
test agent::control::tests::v2_spawn_agent_releases_slot_after_completion ... ok
test agent::control::tests::spawn_agent_releases_slot_after_completion ... FAILED

failures:
---- agent::control::tests::spawn_agent_releases_slot_after_completion stdout ----
thread 'agent::control::tests::spawn_agent_releases_slot_after_completion'
  panicked at core/src/agent/control_tests.rs:1101:6:
completed child should be retired: Elapsed(())

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 1651 filtered out; finished in 10.33s
```

Restore the call:

```
$ cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion
test agent::control::tests::v2_spawn_agent_releases_slot_after_completion ... ok
test agent::control::tests::spawn_agent_releases_slot_after_completion ... ok
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 1651 filtered out; finished in 0.56s
```

The 10-second `Elapsed(())` panic is the test's `timeout(Duration::from_secs(10), …)` waiting for the child to be retired; without the V1 watcher's retire call, the slot never frees and the timeout fires.

### Soak

4 workers × 60 s, V1 `[agents] max_threads = 4`, `spawn_recursive` SSE fixture (summaries at [`artifacts/soak-summaries/`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/tree/main/artifacts/soak-summaries)):

| Metric                                                | Unpatched (`codex-cli 0.125.0`) | Patched (fix branch) |
| ----------------------------------------------------- | ------------------------------- | -------------------- |
| Total `agent thread limit reached` rejections (4×60s) | 184,819                         | 65,571               |
| Per-worker rejection rate                             | ~46k                            | ~16k                 |

64 % drop in rejection rate. Caveats: the binaries differ (unpatched is `0.125.0` release; patched is dev profile of the fix branch), and the soak only measures rejected spawns, not accepted ones — a clean A/B would build both with the same profile and add an explicit accepted-spawn counter. The drop is consistent with retirement freeing slot capacity, but isn't on its own proof of MCP-child termination.

## Out of scope

- `McpConnectionManager` ownership (per-session vs. shared / lazy / pooled) — design decision, not a bug.
- `Shutdown` participation in retirement.
- Integration test asserting MCP child PIDs terminate on retirement (skeleton at [`artifacts/integration-test-skeleton.md`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/integration-test-skeleton.md), would mirror #19753's `process_group_cleanup.rs`).
- Concurrent-shutdown race tests at the control layer beyond the registry-level idempotency tests already included.

Read [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md). Offered as analysis material; not opening a PR. Will follow the invitation process if useful.

— Alejandro Pena ([@adpena](https://github.com/adpena))
