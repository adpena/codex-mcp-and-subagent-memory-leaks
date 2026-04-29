[#19753](https://github.com/openai/codex/pull/19753) (merged 2026-04-28) tears down `McpConnectionManager` for any session that enters shutdown. One structural gap in `codex-rs/core` remains on current `main` ([`80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)):

`AgentRegistry::release_spawned_thread` ([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99)) is called only from `agent/control.rs:693` (`CodexErr::InternalAgentDied`) and `agent/control.rs:714` (`shutdown_live_agent`). `Completed` / `Errored` finalization does not retire a slot, so a finalized subagent's session stays alive holding its `McpConnectionManager`. V1 has a completion watcher gated `!Feature::MultiAgentV2` at `control.rs:307`; V2 has no equivalent.

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# 0 matches
```

Cross-platform reports: [#16828](https://github.com/openai/codex/issues/16828) (Linux: 49.4 GB peak, hard-froze a CachyOS workstation), [#12414](https://github.com/openai/codex/issues/12414) (Windows: 90 GB commit growth → OOM), [#19381](https://github.com/openai/codex/issues/19381) (Windows app + VSCode: 10 GB+), [#18103](https://github.com/openai/codex/issues/18103) (macOS + Ghostty/zellij). The defect is in platform-agnostic Rust code (no `cfg(target_os)` guards on the affected files).

## Suggested fix

Patch: [`pr1-subagent-retention-after-19753.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-after-19753.patch).

- `AgentMetadata.slot_state: SlotState` (`NotTracked` / `Active` / `Retired { last_status }`), replacing `slot_active: bool` + `last_status: Option<AgentStatus>`.
- `AgentRegistry::retire_spawned_thread(id, status)` transitions `Active → Retired` and decrements `total_count` once. Idempotent under repeated retire / interleaved release.
- `AgentControl::retire_finalized_agent` flushes rollout, enqueues `Op::Shutdown`, removes the thread, retires the slot. Invoked from V1's `maybe_start_completion_watcher` and from V2's `Session::maybe_notify_parent_of_terminal_turn` via `tokio::spawn` (synchronous V2 call self-deadlocks: `Session::send_event` is the same loop the submission channel's receiver is on). Retires only `Completed` / `Errored`; `Shutdown` is intentionally left alone (regressed an existing resume-path test).
- `get_status` / `wait_agent` / `list_agents` fall back to cached registry status when no live thread exists. `resume_agent` switches to `has_live_thread()` rather than treating any non-`NotFound` status as proof of non-resumability.
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

$ cargo test -p codex-core --lib
test result: ok. 1648 passed; 0 failed; 3 ignored; 0 measured; 0 filtered out; finished in 31.68s
```

`cargo test --workspace --no-fail-fast` surfaces three target-level failures unrelated to this patch — all reproduce on plain `origin/main`:

- `codex-exec-server::server::handler::tests::{long_poll_read_fails_after_session_resume, output_and_exit_are_retained_after_notification_receiver_closes}` — fail under workspace parallel-test contention, pass individually on either branch.
- `codex-tui --lib` SIGABRT (`signal: 6`) after ~2009 tests, no panic stack, last test varies per run. Reproduces on plain `80fb0704ee` with no patch.
- `codex-core::suite::approvals::approval_matrix_covers_group::workspace_write` exceeds the 60s soft timeout under workspace load; passes individually.

`git grep -nE 'slot_active|\.last_status|slot_state|retire_finalized_agent' codex-rs/tui/` returns zero matches.

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

Restore each call and the test passes in <1s.

## Scope

One defect: slot retention on `Completed` / `Errored` finalization. A descendant-drain in `session::handlers::shutdown` (to clean up live spawned descendants when a root session shuts down outside `ThreadManager::shutdown_all_threads_bounded`) was prototyped and removed: it raced destructively with the bulk-shutdown loop — both paths called `shutdown_live_agent` on the same descendants in parallel, leaving the rollout writer in a state that `resume_agent_from_rollout` couldn't recover from
([`resume_agent_from_rollout_reopens_open_descendants_after_manager_shutdown`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control_tests.rs#L2467),
[`resume_agent_from_rollout_uses_edge_data_when_descendant_metadata_source_is_stale`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control_tests.rs#L2558)).
A clean fix needs cooperation with `ThreadManager` on cleanup authority — larger than this patch.

## Not addressed by this patch

- Per-session `McpConnectionManager` ownership — design decision, not a bug.
- Root-shutdown descendant drain (above).
- `Shutdown` participation in retirement (regresses an existing resume-path test).
- Integration test asserting MCP child PIDs terminate on retirement (skeleton at [`artifacts/integration-test-skeleton.md`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/integration-test-skeleton.md), would mirror #19753's `process_group_cleanup.rs`).

Per [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md), this is offered as analysis. Will follow the invitation process if useful.

— Alejandro Pena ([@adpena](https://github.com/adpena))
