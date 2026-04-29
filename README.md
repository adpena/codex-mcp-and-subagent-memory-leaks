# Codex MCP and subagent memory leaks

An investigation into long-session degradation in the Codex CLI on macOS, especially under
high concurrency with stdio MCP servers and recursive subagents. This repository contains
a root-cause analysis, a reproducible soak harness, and a narrow fix patch that targets the
dominant accumulation path in `codex-core`.

The fix is offered as analysis material rather than an opened upstream PR. If the Codex
maintainers find the direction useful, I'm happy to open it as a PR against
[openai/codex](https://github.com/openai/codex).

## TL;DR

Under recursive subagent spawning + stdio MCP fanout, the Codex CLI on macOS leaks live
agent slots and lets the busiest root session keep more MCP child processes and listener
state than it should. The session with the most subagents tends to destabilize first; in
my environment that surfaced as flicker / jank / `memallocstack`-style failures and
`Ctrl+C` going unresponsive in the affected terminal pane.

The narrow fix in `codex-core`:

- retires `Completed` / `Errored` spawned agents from live slot ownership while preserving
  cached final status for `wait_agent` / `list_agents` / `resume_agent`
- releases reserved nicknames if pre-commit spawn setup fails
- drains live spawned descendants on root-session shutdown with a bounded sweep before and
  after `conversation.shutdown()`, and only clears the spawned-agent registry when no live
  descendants remain detectable

Soak measurements before vs. after the fix on the same `spawn_recursive` scenario:

| Run | `agent_slot_reserved` | `agent_slot_released` | `mcp_spawned` | `mcp_dropped` |
| --- | --------------------- | --------------------- | ------------- | ------------- |
| Before fix (clean upstream `3895ddd6b`) | 6 | **0** | 7 | 7 |
| After fix (PR1 branch, run 1)           | 6 | **4** | 7 | 6 |
| After fix (PR1 branch, run 2)           | 6 | **5** | 7 | 7 |

Raw summaries: [`artifacts/soak-summaries/`](artifacts/soak-summaries/).

## Environment that triggered this

This was reproduced on a real workload, not a synthetic stress test:

- **macOS** (`26.4` / Darwin `25.4.0` / arm64), high-memory machine
- **[Ghostty](https://ghostty.org/)** as the terminal, with **multiple panes open at once**
- in each pane, an interactive `codex` session, often with **recursive subagent spawning**
- a deliberately busy MCP configuration: many stdio servers, including some with
  **buggy or partially broken tool implementations** that didn't always return cleanly
- frequent **failed tool calls** that didn't release their MCP-side resources promptly
- **subagents that were started but never explicitly closed** before the root session
  was interrupted or restarted
- sessions that lived for hours rather than minutes

The failure mode is cumulative. A short session looks healthy. The pane that's been open
the longest with the most subagents and the heaviest MCP traffic is the first to flicker,
jank, mis-render, then lose `Ctrl+C` responsiveness. Once one pane is contaminated,
attempts to recover from neighboring panes can spread the damage as agents try to clean
up each other's processes.

This matches the broader pattern of memory growth and pane corruption that several macOS
+ terminal-emulator users have been reporting recently.

## Where the leak actually lives

Two upstream properties combine to produce the dominant accumulation path:

1. **Spawned-agent slot retention.** Spawned agents reserve a slot in the shared agent
   registry. On clean upstream `3895ddd6b`, that slot is only released when the thread
   metadata is removed — `Completed` / `Errored` finalization alone does not retire a
   slot. So finalized children keep counting against `agents.max_threads` for longer than
   they should.
   - `codex-rs/core/src/agent/registry.rs::AgentRegistry::release_spawned_thread`
   - `codex-rs/core/src/agent/control.rs::AgentControl::get_status`

2. **Per-session MCP fanout.** Each session owns its own `SessionServices`, including its
   own `McpConnectionManager`. Recursive subagent spawning therefore multiplies stdio
   MCP child processes rapidly — the soak shows 1 worker producing 7 MCP children.
   - `codex-rs/core/src/state/service.rs`
   - `codex-rs/core/src/codex.rs`

3. **Incomplete root-session descendant teardown.** `handlers::shutdown(...)` on clean
   upstream shuts down the current session and clears local resources. Under recursive
   fanout, descendants can become observable mid-shutdown and miss the single-pass
   cleanup. Some descendants then escape the first sweep and outlive the root.

The crux: **live descendant shutdown leak + finalized-agent retention + per-session MCP
fanout**. Each on its own is recoverable; combined under recursive churn they make the
busiest root session keep the most live MCP children and agent slots the longest, which
is the session most likely to destabilize first.

## What PR1 fixes

PR1 is intentionally narrow — it targets the agent-registry / control-layer accumulation
path without redesigning MCP ownership.

Files changed (9):

- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/codex.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/wait.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs`
- `codex-rs/core/src/agent/registry_tests.rs` (new tests)
- `codex-rs/core/src/agent/control_tests.rs` (new tests)
- `codex-rs/core/tests/common/lib.rs` (test-only Miri guard on macOS)
- `codex-rs/core/tests/suite/mod.rs` (test-only Miri guard on macOS)

Patch: [`patches/pr1-subagent-retention-root-teardown.patch`](patches/pr1-subagent-retention-root-teardown.patch)
(410 insertions, 23 deletions).

Key changes:

- **Registry retirement.** `AgentMetadata` gains `last_status: Option<AgentStatus>` and
  `slot_active: bool`. A new `retire_spawned_thread(thread_id, status)` caches the final
  status, flips `slot_active = false`, and decrements the live spawned count exactly once.
- **Control-layer retirement.** `AgentControl::retire_finalized_agent` removes the live
  thread, retires slot ownership, and tolerates rollout flush failure. The completion
  watcher only retires `Completed` / `Errored` — `Shutdown` is intentionally left alone
  because broad retirement of shutdown states regressed existing resume-path tests.
- **Cached-status fallbacks.** `get_status`, `wait_agent`, and `list_agents` fall back to
  cached registry status when the live thread is gone, so retirement doesn't break
  collaboration UX. `resume_agent` switches to `has_live_thread(thread_id)` instead of
  treating any non-`NotFound` status as proof of non-resumability.
- **Bounded root-shutdown sweep.** `handlers::shutdown` now does two descendant shutdown
  passes (before and after `conversation.shutdown()`) and only clears the spawned-agent
  registry when `live_thread_spawn_descendants` returns empty.
- **Nickname leak.** `SpawnReservation::Drop` releases the reserved nickname if the spawn
  failed before commit.

A full per-file change dossier with rationale, behavioral diff, and residual risk is in
[`artifacts/pr1-change-dossier.md`](artifacts/pr1-change-dossier.md).

### What PR1 deliberately does *not* fix

- unbounded inter-agent mailbox / queued follow-up backpressure
- per-session MCP process fanout architecture (live sessions still each own a
  `McpConnectionManager`)
- app-server disconnect cleanup for pending server-request callbacks
- terminal restore / alt-screen hardening after catastrophic failure (the TUI side of the
  visible corruption)

These are tracked as PR2 follow-up below.

## Verification

All commands run on the PR1 branch against upstream `3895ddd6b1caf80cd77d6fd44e3ce55bd290ef18`.

Targeted regressions covering the lifecycle changes:

```bash
cargo test -p codex-core resume_agent_respects_max_threads_limit -- --nocapture
cargo test -p codex-core spawn_agent_releases_slot_after_completion -- --nocapture
cargo test -p codex-core root_shutdown_shuts_down_live_spawned_descendants -- --nocapture
cargo test -p codex-core failed_spawn_releases_reserved_nickname -- --nocapture
```

Hygiene:

```bash
cargo fmt --all
cargo clippy -p codex-core --tests -- -D warnings
git diff --check
cargo test -p codex-tui
cargo build -p codex-cli --bin codex
```

Strict-provenance Miri (macOS arm64, nightly):

```bash
MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation' \
  cargo +nightly miri test -p codex-core --lib retire_releases_slot_and_preserves_cached_status
MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation' \
  cargo +nightly miri test -p codex-core --lib failed_spawn_releases_reserved_nickname
```

Both Miri runs pass. The new `cfg(miri)` guards in `tests/common/lib.rs` and
`tests/suite/mod.rs` exist solely so strict-provenance Miri can reach the changed
lifecycle logic on macOS — they no-op test-startup helpers that do `realpath` / `chmod` /
PATH mutation, which strict-provenance Miri rejects before ever entering PR1 code.
Tokio-based handler tests are still blocked under macOS Miri by unsupported `kqueue`
calls in the runtime, which is a pre-existing platform limitation.

### Pre-existing failures observed (not PR1 regressions)

A full `cargo test -p codex-core` run on this machine still surfaces three `tests/all.rs`
failures that I checked are not caused by PR1:

- `suite::realtime_conversation::conversation_webrtc_start_posts_generated_session` — fails
  the same way on a clean upstream clone and does not touch PR1 code paths.
- `suite::subagent_notifications::subagent_notification_is_included_without_wait` — passes
  in isolation on both branches; appears to be long-suite interaction / flakiness.
- `suite::cli_stream::responses_mode_stream_cli_supports_openai_base_url_config_override`
  — passes in isolation on the PR1 branch.

`cargo test -p codex-app-server` is red in two realtime conversation tests
(`realtime_webrtc_start_emits_sdp_notification`,
`webrtc_v1_start_posts_offer_returns_sdp_and_joins_sideband`); both reproduce on the
clean upstream clone and are unrelated to the lifecycle diff.

## How to apply

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

The patch is generated from the working tree against `origin/main` at
`3895ddd6b1caf80cd77d6fd44e3ce55bd290ef18` and contains 9 files / 410 insertions /
23 deletions.

## Reproducing the underlying defect

The full investigation note, including the upstream code references, is in
[`artifacts/repro.md`](artifacts/repro.md). The hostile soak driver used to produce the
before/after numbers above is checked in at
[`artifacts/soak_codex_concurrency.py`](artifacts/soak_codex_concurrency.py). It launches
many `codex exec` workers, assigns hostile fixture scenarios, samples process state, and
emits a `summary.json` per run.

Build prerequisites:

```bash
cd codex-rs
cargo build -p codex-cli --bin codex
cargo build -p codex-rmcp-client --bin test_stdio_server
```

Example run that reproduces the slot-retention pattern:

```bash
./scripts/soak_codex_concurrency.py \
  --workers 1 \
  --duration-sec 6 \
  --spawn-interval-ms 200 \
  --sample-interval-sec 2 \
  --debug-lifecycle \
  --scenarios spawn_recursive
```

Useful lifecycle logging toggles (also set automatically by `--debug-lifecycle`):

- `CODEX_DEBUG_AGENT_LIFECYCLE=1`
- `CODEX_DEBUG_MCP_LIFECYCLE=1`
- `CODEX_DEBUG_THREAD_LISTENERS=1`

## PR2: broader follow-up (planned, not yet implemented)

PR1 reduces the dominant accumulation path. The remaining systems work, tracked in
[`artifacts/pr1-pr2-plan.md`](artifacts/pr1-pr2-plan.md):

1. **Backpressure and queue hygiene.** Bound or meter inter-agent mailbox growth, audit
   `wait_agent` wakeups that fire on mailbox sequence changes even when no real work
   starts, and add explicit queued follow-up storm tests.
2. **App-server disconnect / orphan cleanup.** Resolve pending callback / request
   cleanup when connections disappear mid-flight; audit notification wait paths that
   depend on writer acknowledgements.
3. **Terminal / TUI hardening.** Tighten terminal restore semantics around raw mode and
   the alternate screen; investigate event-loop starvation under high app-event churn —
   this is the most likely contributor to the visible Ghostty pane corruption that
   survives the underlying backend fix.
4. **MCP fanout architecture.** Decide whether spawned sessions should isolate, reuse,
   or lazily create MCP connection managers; measure whether eager per-session MCP
   startup remains too expensive even after PR1.

## Open questions for the Codex team

- Is the retirement model in PR1 also wanted on the `multi_agent_v2` path, which uses
  different completion plumbing?
- Should `Shutdown` participate in retirement (PR1 deliberately leaves it alone — broad
  retirement of shutdown states regressed an existing resume-path test, so this is a
  scope decision rather than a technical blocker)?
- Is the per-session `McpConnectionManager` ownership intentional long-term, or is the
  intent to move toward shared / lazily-created MCP managers? PR2 direction depends on
  the answer.

## Repository layout

```
README.md                                       this document
patches/
  pr1-subagent-retention-root-teardown.patch    apply against upstream 3895ddd6b
artifacts/
  repro.md                                      investigation note + upstream code refs
  pr1-change-dossier.md                         per-file rationale, behavioral diff, risk
  pr1-pr2-plan.md                               PR1 scope / PR2 follow-up tracks
  soak_codex_concurrency.py                     hostile soak driver
  soak-summaries/                               raw before/after lifecycle counts
```

## Contact

Happy to open this as a PR against `openai/codex`, run additional repros, or extend the
soak harness with whatever scenarios are most useful. Reach me at
[github.com/adpena](https://github.com/adpena).
