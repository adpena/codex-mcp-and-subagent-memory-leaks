[#19753](https://github.com/openai/codex/pull/19753) (merged 2026-04-28) tears down `McpConnectionManager` for any session that enters shutdown. One structural gap in `codex-rs/core` remains on current `main` ([`80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)):

`AgentRegistry::release_spawned_thread` ([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99)) is called only from `agent/control.rs:693` (`CodexErr::InternalAgentDied`) and `agent/control.rs:714` (`shutdown_live_agent`). `Completed` / `Errored` finalization does not retire a slot, so a finalized subagent's session stays alive holding its `McpConnectionManager`. V1 has a completion watcher gated `!Feature::MultiAgentV2` at `control.rs:307`; V2 has no equivalent.

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# 0 matches
```

Cross-platform reports: [#16828](https://github.com/openai/codex/issues/16828) (Linux: 49.4 GB peak, hard-froze a CachyOS workstation), [#12414](https://github.com/openai/codex/issues/12414) (Windows: 90 GB commit growth → OOM), [#19381](https://github.com/openai/codex/issues/19381) (Windows app + VSCode: 10 GB+), [#18103](https://github.com/openai/codex/issues/18103) (macOS + Ghostty/zellij). The defect is in platform-agnostic Rust code (no `cfg(target_os)` guards on the affected files).

## Suggested fix

Topic branch [`fix/subagent-retention-after-19753`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/tree/main/codex), patch [`pr1-subagent-retention-after-19753.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-after-19753.patch) (applies cleanly on top of `80fb0704ee`):

- `AgentMetadata.slot_state: SlotState` (`NotTracked` / `Active` / `Retired { last_status }`), replacing the prior `slot_active: bool` + `last_status: Option<AgentStatus>` so the lifecycle is explicit in the type system.
- `AgentRegistry::retire_spawned_thread(id, status)` transitions `Active → Retired`, decrements `total_count` once. Idempotent under repeated retire / interleaved release.
- `AgentControl::retire_finalized_agent` flushes rollout, enqueues `Op::Shutdown` on the session's submission channel (so the session loop drains it into `handlers::shutdown`, picking up #19753's `begin_shutdown()`), removes the live thread, and retires the registry slot. Invoked from V1's `maybe_start_completion_watcher` and from V2's `Session::maybe_notify_parent_of_terminal_turn` via `tokio::spawn` — synchronous V2 invocation self-deadlocks because the call site runs inside `Session::send_event` and the bounded submission channel's receiver is the same loop. The completion watcher and V2 path retire only `Completed` / `Errored`. `Shutdown` is intentionally not retired (regressed an existing resume-path test).
- `get_status` / `wait_agent` / `list_agents` fall back to cached registry status when no live thread exists. `resume_agent` switches to `has_live_thread()` instead of treating any non-`NotFound` status as proof of non-resumability.
- `SpawnReservation::Drop` releases the reserved nickname when spawn setup fails before commit.

## Verification

Fresh clone of `openai/codex@80fb0704ee` with the patch applied:

```
$ git apply --check patches/pr1-subagent-retention-after-19753.patch
$ cargo fmt --all -- --check
$ cargo clippy --workspace --tests -- -D warnings
    Finished `dev` profile [unoptimized + debuginfo] target(s) in 23.22s

$ cargo test -p codex-core --lib retire_releases_slot_and_preserves_cached_status
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1650 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion
test result: ok. 2 passed; 0 failed; 0 ignored; 0 measured; 1649 filtered out; finished in 0.68s

$ cargo test -p codex-core --lib v2_spawn_agent_releases_slot_after_completion
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1650 filtered out; finished in 0.49s

$ cargo test -p codex-core --lib failed_spawn_releases_reserved_nickname
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1650 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib retire_after_release_does_not_double_decrement
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1650 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib retire_is_idempotent_for_repeated_calls
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1650 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib release_after_retire_does_not_double_decrement
test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 1650 filtered out; finished in 0.00s

$ cargo test -p codex-core --lib   # workspace-equivalent for the changed crate
test result: ok. 1648 passed; 0 failed; 3 ignored; 0 measured; 0 filtered out; finished in 31.68s
```

`cargo test --workspace --no-fail-fast` on the same fresh clone surfaces three target-level failures, all in code untouched by this patch and reproducible on `origin/main` without the patch when run under parallel-test contention: two `codex-exec-server::server::handler::tests` (`long_poll_read_fails_after_session_resume`, `output_and_exit_are_retained_after_notification_receiver_closes`) — both pass when re-run individually; a `codex-tui --lib` SIGABRT after 2018 tests (process-level abort, no specific test); and `codex-core::suite::approvals::approval_matrix_covers_group::workspace_write` exceeding the 60s soft timeout. None of these touch `agent/`, `session/handlers.rs`, or any other file the patch modifies.

### Failing-then-passing demonstration

V1: comment out the V1 retirement call in `agent/control.rs::maybe_start_completion_watcher`:

```
$ cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion
test agent::control::tests::v2_spawn_agent_releases_slot_after_completion ... ok
test agent::control::tests::spawn_agent_releases_slot_after_completion ... FAILED

failures:
---- agent::control::tests::spawn_agent_releases_slot_after_completion stdout ----
thread 'agent::control::tests::spawn_agent_releases_slot_after_completion'
  panicked at core/src/agent/control_tests.rs:1101:6:
completed child should be retired: Elapsed(())

test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 1649 filtered out; finished in 10.33s
```

V2: comment out the V2 retirement spawn in `session/mod.rs::maybe_notify_parent_of_terminal_turn`:

```
$ cargo test -p codex-core --lib v2_spawn_agent_releases_slot_after_completion
test agent::control::tests::v2_spawn_agent_releases_slot_after_completion ... FAILED

failures:
---- agent::control::tests::v2_spawn_agent_releases_slot_after_completion stdout ----
thread 'agent::control::tests::v2_spawn_agent_releases_slot_after_completion'
  panicked at core/src/agent/control_tests.rs:1205:6:
completed V2 child should be retired: Elapsed(())

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 1652 filtered out; finished in 10.37s
```

Restore both calls and the tests pass in <1s. The 10-second `Elapsed(())` is the test's `timeout(Duration::from_secs(10), …)` waiting for retirement.

## Scope

This patch addresses one defect: slot retention on `Completed` / `Errored` finalization. An earlier draft also added a descendant-drain in `session::handlers::shutdown` to clean up live spawned descendants when a root session shuts down without going through `ThreadManager::shutdown_all_threads_bounded`. That implementation raced destructively with the bulk-shutdown loop — both paths called `shutdown_live_agent` on the same descendants in parallel, leaving the rollout writer in a state that `resume_agent_from_rollout` couldn't recover from. The two failing regression tests
([`resume_agent_from_rollout_reopens_open_descendants_after_manager_shutdown`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control_tests.rs#L2467),
[`resume_agent_from_rollout_uses_edge_data_when_descendant_metadata_source_is_stale`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control_tests.rs#L2558))
surface the regression. Cleanly fixing the descendant-drain needs cooperation with `ThreadManager` so the two paths agree on cleanup authority — that's larger than this patch.

## Not addressed by this patch

- Per-session `McpConnectionManager` ownership — design decision, not a bug.
- Root-shutdown descendant drain (above).
- `Shutdown` participation in retirement (regresses an existing resume-path test).
- Integration test asserting MCP child PIDs terminate on retirement (skeleton at [`artifacts/integration-test-skeleton.md`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/integration-test-skeleton.md), would mirror #19753's `process_group_cleanup.rs`).

Read [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md). Offered as analysis material; not opening a PR. Will follow the invitation process if useful.

— Alejandro Pena ([@adpena](https://github.com/adpena))
