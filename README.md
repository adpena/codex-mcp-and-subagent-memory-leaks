# Codex MCP and subagent memory leaks

Investigation, reproducible soak, and a `codex-core` patch for the long-session
degradation seen on macOS under recursive subagents and stdio MCP fanout.

Per [`openai/codex`'s contributing policy](https://github.com/openai/codex/blob/main/docs/contributing.md),
external code contributions are by invitation only and unsolicited PRs are closed
without review. This repo is analysis material in the spirit of that policy — issue
fodder, not an unsolicited PR. If the team would find a PR useful, I'll open one
when invited.

cc, based on recent authorship of the touched files:

- [@jif-oai](https://github.com/jif-oai) — primary author on `core/src/agent/control.rs`
  and `core/src/tools/handlers/multi_agents/`
- [@pakrym-oai](https://github.com/pakrym-oai) — primary author on
  `core/src/codex.rs` (shutdown handlers)
- [@bolinfest](https://github.com/bolinfest) — broad ownership across `codex-rs/core/`
- [@tibo-openai](https://github.com/tibo-openai) — author of the adjacent mailbox PR
  [#17749](https://github.com/openai/codex/pull/17749)

CODEOWNERS team: `@openai/codex-core-agent-team`.

## Summary

Three behaviors compound under recursive subagent + stdio MCP load:

1. `Completed` / `Errored` spawned agents keep their registry slot until thread
   metadata is removed. They count against `agents.max_threads` after they're done.
2. Each session owns its own `McpConnectionManager`, so recursive spawning
   multiplies stdio MCP child processes (1 worker → 7 children in the soak below).
3. `handlers::shutdown` does a single descendant pass; descendants that become
   observable mid-shutdown can outlive the root.

Soak measurements on the same `spawn_recursive` scenario:

| Run                                       | `slot_reserved` | `slot_released` | `mcp_spawned` | `mcp_dropped` |
| ----------------------------------------- | --------------- | --------------- | ------------- | ------------- |
| Clean upstream `3895ddd6b`                | 6               | **0**           | 7             | 7             |
| Patched, run 1                            | 6               | **4**           | 7             | 6             |
| Patched, run 2                            | 6               | **5**           | 7             | 7             |

Raw summaries: [`artifacts/soak-summaries/`](artifacts/soak-summaries/).

## Environment

- macOS 26.4 / Darwin 25.4.0 / arm64, high-memory machine
- Ghostty, several panes open at once
- each pane: an interactive `codex` session, often spawning recursive subagents
- MCP config with many stdio servers; some tools occasionally hang or fail without
  returning cleanly
- subagents frequently started and not explicitly closed before the root session
  was interrupted or restarted
- sessions running for hours

User-visible failure mode: cumulative. The pane with the most subagents and the
heaviest MCP traffic flickers, janks, mis-renders, then loses `Ctrl+C`. Recovery
attempts from neighboring panes can spread damage as agents kill each other's
processes.

## Where the leak lives

Slot retention — `release_spawned_thread` decrements the live spawned-agent count
only when thread metadata is removed. Finalization alone does not retire a slot.

- `codex-rs/core/src/agent/registry.rs::AgentRegistry::release_spawned_thread`
- `codex-rs/core/src/agent/control.rs::AgentControl::get_status`

Per-session MCP fanout — each session's `SessionServices` owns a separate
`McpConnectionManager`. Recursive spawning therefore multiplies stdio children.

- `codex-rs/core/src/state/service.rs`
- `codex-rs/core/src/codex.rs`

Single-pass root teardown — `handlers::shutdown` shuts down the current session
and clears local resources in one pass, which under recursive fanout misses
descendants that only become observable mid-shutdown.

The combination, not any one of these alone, makes the busiest root session the
first to destabilize.

## Patch (`patches/pr1-subagent-retention-root-teardown.patch`)

9 files, 410 insertions, 23 deletions, against `3895ddd6b`.

- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/codex.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/wait.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs`
- `codex-rs/core/src/agent/registry_tests.rs` (new tests)
- `codex-rs/core/src/agent/control_tests.rs` (new tests)
- `codex-rs/core/tests/common/lib.rs` (`cfg(miri)` guards on macOS)
- `codex-rs/core/tests/suite/mod.rs` (`cfg(miri)` guards on macOS)

Changes:

- `AgentMetadata` gains `last_status: Option<AgentStatus>` and `slot_active: bool`.
- `AgentRegistry::retire_spawned_thread(thread_id, status)` caches the final
  status, flips `slot_active = false`, and decrements the live spawned count once.
- `AgentControl::retire_finalized_agent` removes the live thread, retires the
  slot, and tolerates rollout flush failure. The completion watcher retires only
  `Completed` / `Errored`. Broad retirement of `Shutdown` regressed an existing
  resume-path test, so it's left alone — flagged as an open question below.
- `get_status`, `wait_agent`, and `list_agents` fall back to cached registry
  status when the live thread is gone. `resume_agent` switches to
  `has_live_thread(thread_id)` instead of treating any non-`NotFound` status as
  proof of non-resumability.
- `handlers::shutdown` does two descendant shutdown passes (before and after
  `conversation.shutdown()`) and clears the spawned-agent registry only when
  `live_thread_spawn_descendants` returns empty.
- `SpawnReservation::Drop` releases the reserved nickname if spawn setup fails
  before commit.

Per-file behavioral diff and residual risk:
[`artifacts/pr1-change-dossier.md`](artifacts/pr1-change-dossier.md).

Out of scope: inter-agent mailbox backpressure, per-session MCP ownership
redesign, app-server disconnect cleanup, terminal restore semantics. These are
PR2 follow-up tracks in [`artifacts/pr1-pr2-plan.md`](artifacts/pr1-pr2-plan.md).

## Verification

All on the patched branch against `3895ddd6b`.

```bash
cargo test -p codex-core resume_agent_respects_max_threads_limit -- --nocapture
cargo test -p codex-core spawn_agent_releases_slot_after_completion -- --nocapture
cargo test -p codex-core root_shutdown_shuts_down_live_spawned_descendants -- --nocapture
cargo test -p codex-core failed_spawn_releases_reserved_nickname -- --nocapture
cargo fmt --all
cargo clippy -p codex-core --tests -- -D warnings
git diff --check
cargo test -p codex-tui
cargo build -p codex-cli --bin codex
```

Strict-provenance Miri (nightly, macOS arm64):

```bash
MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation' \
  cargo +nightly miri test -p codex-core --lib retire_releases_slot_and_preserves_cached_status
MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation' \
  cargo +nightly miri test -p codex-core --lib failed_spawn_releases_reserved_nickname
```

The `cfg(miri)` guards in `tests/common/lib.rs` and `tests/suite/mod.rs` no-op
test-startup helpers that do `realpath` / `chmod` / `PATH` mutation, which
strict-provenance Miri rejects before reaching the lifecycle code. Tokio handler
tests are still blocked under macOS Miri by unsupported `kqueue` calls — a
pre-existing platform limitation, unrelated to this patch.

### Pre-existing failures (verified unrelated)

A full `cargo test -p codex-core` surfaces three `tests/all.rs` failures:

- `suite::realtime_conversation::conversation_webrtc_start_posts_generated_session`
  — fails the same way on a clean upstream clone.
- `suite::subagent_notifications::subagent_notification_is_included_without_wait`
  — passes in isolation on both branches; long-suite interaction.
- `suite::cli_stream::responses_mode_stream_cli_supports_openai_base_url_config_override`
  — passes in isolation on the patched branch.

`cargo test -p codex-app-server` is red in two realtime conversation tests
(`realtime_webrtc_start_emits_sdp_notification`,
`webrtc_v1_start_posts_offer_returns_sdp_and_joins_sideband`); both reproduce on
clean upstream.

## Apply

```bash
git clone https://github.com/openai/codex.git
cd codex
git checkout 3895ddd6b1caf80cd77d6fd44e3ce55bd290ef18
git apply /path/to/patches/pr1-subagent-retention-root-teardown.patch
cd codex-rs
cargo fmt --all
cargo clippy -p codex-core --tests -- -D warnings
cargo test -p codex-core spawn_agent_releases_slot_after_completion -- --nocapture
cargo test -p codex-core root_shutdown_shuts_down_live_spawned_descendants -- --nocapture
```

## Reproduce

Full investigation note with upstream code references:
[`artifacts/repro.md`](artifacts/repro.md). Hostile soak driver:
[`artifacts/soak_codex_concurrency.py`](artifacts/soak_codex_concurrency.py).

```bash
cd codex-rs
cargo build -p codex-cli --bin codex
cargo build -p codex-rmcp-client --bin test_stdio_server
```

```bash
./scripts/soak_codex_concurrency.py \
  --workers 1 \
  --duration-sec 6 \
  --spawn-interval-ms 200 \
  --sample-interval-sec 2 \
  --debug-lifecycle \
  --scenarios spawn_recursive
```

Lifecycle logging (also set automatically by `--debug-lifecycle`):

- `CODEX_DEBUG_AGENT_LIFECYCLE=1`
- `CODEX_DEBUG_MCP_LIFECYCLE=1`
- `CODEX_DEBUG_THREAD_LISTENERS=1`

## Open questions

- Should this retirement model apply on the `multi_agent_v2` path? It uses
  different completion plumbing.
- Should `Shutdown` participate in retirement? Broad retirement of shutdown
  states regressed an existing resume-path test, so the patch leaves it alone.
- Is per-session `McpConnectionManager` ownership the long-term shape, or is the
  intent shared / lazily-created managers? PR2 direction depends on the answer.

## Layout

```
README.md                                       this document
patches/
  pr1-subagent-retention-root-teardown.patch    apply against upstream 3895ddd6b
artifacts/
  repro.md                                      investigation note + upstream code refs
  pr1-change-dossier.md                         per-file rationale and residual risk
  pr1-pr2-plan.md                               PR1 scope, PR2 follow-up tracks
  soak_codex_concurrency.py                     hostile soak driver
  soak-summaries/                               raw before/after lifecycle counts
```
