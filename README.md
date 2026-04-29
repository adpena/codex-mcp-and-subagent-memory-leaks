# Codex MCP and subagent memory leaks

Investigation, reproducible soak, and a `codex-core` patch for the long-session
degradation seen across macOS, Linux, and Windows under recursive subagents and
stdio MCP fanout. The defects are in platform-agnostic Rust code (no
`cfg(target_os)` guards on any of the seven affected files); user-visible
symptoms vary by terminal emulator and OS, but the underlying memory / process
leak is universal.

Per [`openai/codex`'s contributing policy](https://github.com/openai/codex/blob/main/docs/contributing.md),
external code contributions are by invitation only and unsolicited PRs are closed
without review. This repo is analysis material in the spirit of that policy — issue
fodder, not an unsolicited PR. If the team would find a PR useful, I'll open one
when invited.

## Verification against current upstream (timeline)

This analysis has had to track a moving target — relevant fixes have been
landing in upstream while the writeup was being prepared.

| Reference point | Commit / version | Date | Status of the three defects |
| --- | --- | --- | --- |
| Original investigation | [`3895ddd6b`](https://github.com/openai/codex/commit/3895ddd6b1caf80cd77d6fd44e3ce55bd290ef18) | 2026-04-12 | All three present |
| Reporter's installed CLI | `codex-cli 0.125.0` (release `rust-v0.125.0`) | 2026-04-24 | All three present |
| Current `main` re-read | [`80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b) | 2026-04-28 | #1 present, #2 present, **#3 partially fixed by [#19753](https://github.com/openai/codex/pull/19753)** |

[#19753](https://github.com/openai/codex/pull/19753) ("Terminate stdio MCP
servers on shutdown to avoid process leaks", merged 2026-04-28) explicitly adds
`mcp_connection_manager.begin_shutdown()` to `session::handlers::shutdown` and
to the implicit-shutdown path in `submission_loop`, plus extensive
process-group cleanup in
[`stdio_server_launcher.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/rmcp-client/src/stdio_server_launcher.rs).
It explicitly Fixes #12491, #12976, #18881, #19469. **`codex-cli 0.125.0`
predates this merge by four days, so the user-visible behavior on the latest
released CLI is still the unfixed shape.**

Defects on current `main` after #19753:

- **#1 — Spawned-agent slot retention. Unchanged.**
  `AgentRegistry::release_spawned_thread`
  ([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99))
  still decrements only on metadata removal; its two callers in
  `agent/control.rs`
  ([691](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L691),
  [714](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L714))
  trigger only on `InternalAgentDied` and explicit `shutdown_live_agent` —
  `Completed` / `Errored` finalization does not retire a slot. #19753 does not
  modify `agent/registry.rs` or `agent/control.rs`.
- **#2 — Per-session `McpConnectionManager` ownership. Architecturally
  unchanged.**
  Each session still owns its own connection manager
  ([`session/handlers.rs:884`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L884)).
  Recursive subagent spawning still multiplies stdio MCP child processes; the
  fresh soak below confirms ~7 stdio MCP children per worker. #19753 mitigates
  the *symptom* by terminating those processes more reliably at shutdown, but
  does not redesign ownership so they aren't created in the first place per
  recursive descendant.
- **#3 — Single-pass root-session teardown. Partially addressed by #19753 for
  MCP servers; the live spawned-descendant traversal is still not invoked.**
  `session::handlers::shutdown`
  ([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879))
  now calls `mcp_connection_manager.begin_shutdown()` (good) but still does
  not call `live_thread_spawn_descendants` (which exists at
  [`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164)
  and is used elsewhere by `close_agent`). Live spawned subagent descendants
  that become observable mid-shutdown can still outlive the root.

### Codebase changes that affected our reproduction (not the bug)

Two upstream changes between `3895ddd6b` and current `main` make the original
soak harness less informative without adaptation:

- **The `codex.rs` module was split into `session/{handlers.rs, mod.rs, …}`**
  by [#18244](https://github.com/openai/codex/pull/18244) ("Split codex session
  modules") and [#18249](https://github.com/openai/codex/pull/18249) ("Move
  codex module under session"). The patch in this repo was authored against
  the pre-split layout, so its diff anchors need re-targeting onto the new
  paths; the substantive change is unchanged.
- **The lifecycle log strings the soak harness greps for**
  (`"reserved spawned-agent slot"`, `"released spawned-agent slot"`,
  `"spawned stdio MCP server process"`, `"dropping MCP process-group guard"`)
  **were never part of upstream openai/codex** — they were diagnostic
  instrumentation added to the build during the original investigation but
  not preserved in this repo's PR1 patch. Without the instrumentation, the
  harness counters report `0` even when the underlying events occur. Process
  sampling via `ps` still works, which is what the fresh refresh below relies
  on.
- **Adjacent**: [#17749](https://github.com/openai/codex/pull/17749) by
  [@tibo-openai](https://github.com/tibo-openai)
  ("drain mailbox only at request boundaries") sits in similar lifecycle
  territory but does not address registry retirement or descendant drain.

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

### Soak telemetry

Original investigation, against
[`3895ddd6b`](https://github.com/openai/codex/commit/3895ddd6b1caf80cd77d6fd44e3ce55bd290ef18),
on the `spawn_recursive` scenario with the lifecycle hooks the harness was
designed for:

| Run                                       | `slot_reserved` | `slot_released` | `mcp_spawned` | `mcp_dropped` |
| ----------------------------------------- | --------------- | --------------- | ------------- | ------------- |
| Clean upstream `3895ddd6b`                | 6               | **0**           | 7             | 7             |
| Patched, run 1                            | 6               | **4**           | 7             | 6             |
| Patched, run 2                            | 6               | **5**           | 7             | 7             |

Refresh against current `codex-cli 0.125.0` (6 workers × 90 s, summary at
[`artifacts/soak-summaries/current-main-20260428-212049/`](artifacts/soak-summaries/current-main-20260428-212049/)):

- The `CODEX_DEBUG_AGENT_LIFECYCLE` / `CODEX_DEBUG_MCP_LIFECYCLE` /
  `CODEX_DEBUG_THREAD_LISTENERS` env vars and the corresponding lifecycle log
  strings (`"reserved spawned-agent slot"`, etc.) have been removed in
  upstream, so the slot/MCP counters report `0` across the board on the new
  CLI — the harness needs new telemetry hooks to recover those counts.
- `spawn_agent` is now feature-gated; the SSE fixture used by the harness
  drives `function_call: spawn_agent` events that the new tool router
  rejects as `unsupported call: spawn_agent` (50,292 such errors in
  `worker-00` alone). The harness needs a config flag or fixture update to
  drive the V1/V2 handler on current `main`.
- What remains visible in the refresh:
  - **44 stdio MCP child processes across 6 workers (≈7 per worker)** during
    steady state, sampled by `ps`. This confirms **defect #2 (per-session
    `McpConnectionManager` fanout) is still present in `0.125.0`.**
  - During shutdown the launcher emits
    `Failed to terminate MCP process group … No such process` warnings,
    suggesting the cleanup path races with process-group teardown.
- Defects #1 (slot retention) and #3 (root-shutdown drain) require working
  recursive `spawn_agent` to manifest behaviorally. With the current harness
  blocked on the gating change, the verification for those defects is the
  source-level analysis below plus the suggested unit tests in
  [`artifacts/upstream-issue-draft.md`](artifacts/upstream-issue-draft.md).

Raw summaries:
[`artifacts/soak-summaries/`](artifacts/soak-summaries/).

## Reporter's environment

This is the environment in which I personally reproduced the failure mode. It
is not the only environment affected — see "Related upstream reports" below.

- macOS 26.4 / Darwin 25.4.0 / arm64, high-memory machine
- Ghostty 1.3.1, several panes open at once
- each pane: an interactive `codex-cli 0.125.0` session, often spawning
  recursive subagents
- MCP config with many stdio servers; some tools occasionally hang or fail
  without returning cleanly
- subagents frequently started and not explicitly closed before the root
  session was interrupted or restarted
- sessions running for hours
- ChatGPT Pro subscription, latest Codex model

User-visible failure mode: cumulative, in approximately the order observed:

1. The pane with the most subagents and the heaviest MCP traffic begins to
   render strangely — flicker, jank, partial / mis-rendered TUI frames.
2. Process accounting drifts: `ps` shows growing counts of orphaned `codex`
   and stdio MCP server children whose root session was thought to have ended.
3. System-level allocator pressure surfaces: `memallocstack` failures appear,
   APFS swapfile creation fails with "no space left on device" / system
   reports memory pressure (matches [#18103](https://github.com/openai/codex/issues/18103)
   and [#16828](https://github.com/openai/codex/issues/16828)).
4. The contaminated pane stops responding to `Ctrl+C`. Once a pane is in this
   state, recovery attempts from neighboring panes can spread damage —
   surviving agents may try to kill each other's processes while reconciling.
5. In the worst cases reported in upstream issues, the host watchdog-panics
   ([#18103](https://github.com/openai/codex/issues/18103)) or freezes
   entirely ([#16828](https://github.com/openai/codex/issues/16828),
   [#16866](https://github.com/openai/codex/issues/16866)).

### Worst-offender MCP servers (anecdotal)

In the reporter's experience, **browser-automation stdio MCP servers
(`@playwright/mcp`, `chrome-devtools` MCPs, similar)** trigger the cumulative
failure mode the fastest, presumably because each child process drags a
headless browser process tree along with it (Chromium renderer, GPU process,
network service). [#17832](https://github.com/openai/codex/issues/17832)
documents the same Playwright-specific failure mode quantitatively (213
`@playwright/mcp` + `node playwright-mcp` pairs accumulating to 13.6 GB RSS
under multi-agent workflows). The structural defects below are not specific
to any MCP server vendor — heavyweight servers just amplify the leak's RSS
cost — but browser-automation servers are the most reliably reproducible
trigger.

## Related upstream reports (cross-platform)

These existing `openai/codex` issues describe symptoms consistent with the
defects below, on every supported OS:

- **macOS** — [#17832](https://github.com/openai/codex/issues/17832)
  ("Regression: Playwright MCP stdio processes still leak after #16895 fix —
  213 orphaned pairs, 13.6 GB RSS"; `codex-cli 0.120.0`, ChatGPT Pro);
  [#18103](https://github.com/openai/codex/issues/18103)
  (zellij/Ghostty, watchdog panic);
  [#18589](https://github.com/openai/codex/issues/18589)
  (abnormally high RAM, Mac app);
  [#19333](https://github.com/openai/codex/issues/19333)
  (Mac app high memory after update);
  [#16866](https://github.com/openai/codex/issues/16866)
  (`os_refcnt` overflow → kernel panic on Apple Silicon).
- **Linux** — [#16828](https://github.com/openai/codex/issues/16828)
  (CachyOS / kitty, **49.4 GB peak**, hard-froze a 64 GB workstation);
  [#18041](https://github.com/openai/codex/issues/18041)
  (WSL OOM → full system crash).
- **Windows** — [#19381](https://github.com/openai/codex/issues/19381)
  (Windows app + VS Code extension, **10 GB+** RAM after update);
  [#12414](https://github.com/openai/codex/issues/12414)
  (`codex-cli 0.104.0`, **90 GB** commit growth → system OOM);
  [#19293](https://github.com/openai/codex/issues/19293)
  (sandbox process, heavy disk I/O / system lag).
- **Tool / fanout pattern** — [#19600](https://github.com/openai/codex/issues/19600)
  (Python tool calls, **135 GB RAM / 25 GB swap** after long session);
  [#17574](https://github.com/openai/codex/issues/17574)
  (xcodebuild / chrome-devtools MCP leak);
  [#12491](https://github.com/openai/codex/issues/12491)
  (37 GB / 1300+ zombies — original report referenced by #17832).

#17832 is the closest active thread to the analysis here — it identifies the
same MCP teardown path as the failure mode and references the prior partial
fix in [#16895](https://github.com/openai/codex/pull/16895). The substantive
hand-off is intended to be a comment on that issue (draft at
[`artifacts/comment-on-17832-draft.md`](artifacts/comment-on-17832-draft.md)).

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
