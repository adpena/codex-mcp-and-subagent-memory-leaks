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

Two structural defects in `codex-rs/core` cause finalized spawned subagents
to retain registry slots and own session resources past the point they
should have entered shutdown. After [PR #19753](https://github.com/openai/codex/pull/19753)
(merged 2026-04-28) deterministically tears down `McpConnectionManager` for
any session that *enters* shutdown, the remaining gap is making the
finalized subagent enter shutdown at all:

1. **Slot retention on finalization.** `Completed` / `Errored` finalization
   (mapped from `TurnComplete` / `TurnAborted` in `agent/status.rs`) does
   not retire the spawned slot. A finalized subagent's session stays
   alive, counts against `agents.max_threads`, and continues to own its
   `McpConnectionManager` until something else triggers removal. This
   applies to both `multi_agents` (V1) and `multi_agents_v2` (V2), via
   different paths.
2. **No live-descendant drain on root shutdown.**
   `session::handlers::shutdown` doesn't call
   `live_thread_spawn_descendants` — even though that traversal already
   exists at `agent/control.rs:1164` and is used elsewhere by
   `close_agent`. Subagent threads that are live mid-teardown therefore
   never enter their own shutdown path.

### Out of scope (intentionally)

The architectural question of `McpConnectionManager` ownership
(per-session as today vs. shared / lazy / pooled) is a *design* decision,
not a *defect*. Per-session ownership produces fanout while subagents are
**live**, not just after they're finalized; closing defect #1 above only
shortens the post-finalization tail. Picking a new ownership shape
deserves its own proposal where the tradeoffs (isolation,
restart-on-config-change, resource cost) can be evaluated independently.

### Soak telemetry

Provenance: the `slot_reserved`/`released`/`mcp_spawned`/`mcp_dropped`
counters that appear in the historical summaries below come from
diagnostic instrumentation added to a private build during the original
investigation. **They were never part of upstream `openai/codex`.** Re-runs
against unmodified upstream binaries produce zeros for those counters
regardless of fix status; recovering them requires re-applying the
diagnostic patch or wiring a new telemetry surface.

| Provenance | Run                                       | `slot_reserved` | `slot_released` | `mcp_spawned` | `mcp_dropped` |
| ---------- | ----------------------------------------- | --------------- | --------------- | ------------- | ------------- |
| Investigation, `3895ddd6b` + diagnostic build | Clean upstream | 6 | **0** | 7 | 7 |
| Investigation, `3895ddd6b` + patch + diagnostic build | Patched, run 1 | 6 | **4** | 7 | 6 |
| Investigation, `3895ddd6b` + patch + diagnostic build | Patched, run 2 | 6 | **5** | 7 | 7 |

Refresh against `codex-cli 0.125.0` and against the patched fix branch
(2026-04-29, 4 workers × 60 s, `spawn_recursive` scenario, V1 multi_agents
config with `[agents] max_threads = 4`):

| Metric (4 workers × 60 s, `spawn_recursive`)            | Unpatched (`codex-cli 0.125.0`) | Patched (`fix/subagent-retention-after-19753`) | Change   |
| ------------------------------------------------------- | ------------------------------- | ---------------------------------------------- | -------- |
| `unsupported call: spawn_agent` errors (legacy gating)  | 0                               | 0                                              | ✓ both runs use the new V1 config |
| Total `agent thread limit reached` rejections (4 × 60s) | **184,819**                     | **65,571**                                     | **−64%** |
| Per-worker rejection rate                               | ≈ 46,200                        | ≈ 16,400                                       | −65%     |
| Process-sample mean (steady state)                      | 25.0                            | 24.1                                           | ≈ same   |
| Surviving codex / MCP processes after teardown          | 0                               | 0                                              | ≈ same   |

Reading (with caveats below): under the same `spawn_recursive` SSE
fixture and the same `max_threads = 4` cap, the patched binary's
rejection rate drops from ~46k/worker/min to ~16k/worker/min — a 64 %
reduction. The patched binary is rejecting fewer `spawn_agent` calls
per unit time, which is consistent with the patch-driven retirement
releasing slot capacity that finalized subagents previously held.

What the soak does **not** prove, and shouldn't be cited as proving:

- **Apples-to-apples isn't perfectly clean.** Unpatched is `codex-cli
  0.125.0` (release profile, predates #19753); patched is the fix
  branch built with `--profile dev` (debug profile, runs slower).
  Replay rates may differ for reasons unrelated to retirement.
- **Soak measures rejected spawns, not accepted ones.** A drop in
  rejection rate is consistent with retirement freeing slots, but it
  could in principle also reflect a slower replay rate or different
  SSE-channel buffering on debug vs release. A clean A/B would build
  both binaries with the same profile and add an explicit
  accepted-spawn counter to the harness.
- **Per-MCP-child termination on retirement is not asserted here.**
  Per-worker steady-state process counts are dominated by the root
  session's `McpConnectionManager` startup, which both runs share. The
  cleaner verification is an integration test mirroring
  [#19753](https://github.com/openai/codex/pull/19753)'s
  `process_group_cleanup.rs` using `MCP_TEST_PID_FILE`. See pending
  follow-up below.
- **Concurrent shutdown / retirement races aren't covered by the
  soak.** Pending follow-up.

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

## Patch

Two patch files are provided:

- [`patches/pr1-subagent-retention-after-19753.patch`](patches/pr1-subagent-retention-after-19753.patch)
  — **canonical**, applies on top of [`openai/codex@80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)
  (current `main` after [#19753](https://github.com/openai/codex/pull/19753)).
  Generated via `git format-patch` from three squashed commits on
  branch `fix/subagent-retention-after-19753` off `origin/main`. The
  earlier 9-file / 409+ / 23- shape grew through two adversarial-review
  passes: explicit `SlotState` lifecycle enum (replacing
  `slot_active: bool` + `last_status: Option<AgentStatus>`), V2
  retirement wired into `Session::maybe_notify_parent_of_terminal_turn`
  via `tokio::spawn` (the synchronous version self-deadlocked on the
  bounded submission channel), explicit `Op::Shutdown` enqueue from
  `retire_finalized_agent` (so #19753's MCP teardown picks up retired
  sessions instead of relying on `Arc<CodexThread>` drop timing), hard
  deadline-bounded descendant sweep (30 s wall + 64-sweep cap, with
  `tokio::time::timeout_at` wrapping each `shutdown_live_agent` so a
  stuck descendant cannot block the deadline), and new V2 retirement
  + race tests.
- [`patches/pr1-subagent-retention-root-teardown.patch`](patches/pr1-subagent-retention-root-teardown.patch)
  — original PR1 patch against `3895ddd6b` (the investigation commit), kept
  for historical reference and provenance.

Verification on the canonical patch (against `openai/codex@80fb0704ee` on
branch `fix/subagent-retention-after-19753`):

- `git apply --check patches/pr1-subagent-retention-after-19753.patch` — clean.
- `cargo fmt --all -- --check` — clean.
- `cargo clippy -p codex-core --tests -- -D warnings` — clean (no warnings).
- `cargo test -p codex-core --lib retire_releases_slot_and_preserves_cached_status` — passes.
- `cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion` — passes.
- `cargo test -p codex-core --lib root_shutdown_shuts_down_live_spawned_descendants` — passes.
- `cargo test -p codex-core --lib failed_spawn_releases_reserved_nickname` — passes.

The patch composes cleanly with #19753 (no overlapping hunks), modifying 9
files:

- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/codex.rs` (in original) → `codex-rs/core/src/session/handlers.rs`
  (in re-targeted, after the `codex.rs` → `session/{handlers.rs, …}` split in
  PRs [#18244](https://github.com/openai/codex/pull/18244) and
  [#18249](https://github.com/openai/codex/pull/18249))
- `codex-rs/core/src/tools/handlers/multi_agents/wait.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs`
- `codex-rs/core/src/agent/registry_tests.rs` (new tests)
- `codex-rs/core/src/agent/control_tests.rs` (new tests)
- `codex-rs/core/tests/common/lib.rs` (`cfg(miri)` guards on macOS)
- `codex-rs/core/tests/suite/mod.rs` (`cfg(miri)` guards on macOS)

Changes:

- `AgentMetadata.slot_state: SlotState` (explicit
  `NotTracked` / `Active` / `Retired { last_status }`), replacing the prior
  `slot_active: bool` + `last_status: Option<AgentStatus>` pair so the
  three lifecycle states are distinguishable in the type system.
- `AgentRegistry::retire_spawned_thread(thread_id, status)` transitions
  `Active → Retired { last_status }` and decrements the live spawned count
  exactly once. Idempotent under repeated retire / interleaved release
  (covered by the registry-level race tests below).
- `AgentControl::retire_finalized_agent` flushes rollout, enqueues
  `Op::Shutdown` on the session's submission channel (so
  `session::handlers::shutdown` runs once the session loop drains it,
  picking up #19753's `mcp_connection_manager.begin_shutdown()`),
  removes the live thread, and retires the registry slot. Tolerates
  rollout flush failure and channel-closed errors. Invoked from V1's
  `maybe_start_completion_watcher` (existing) and from V2's
  `Session::maybe_notify_parent_of_terminal_turn` via `tokio::spawn` so
  the call cannot self-deadlock on the same session's bounded submission
  channel. The completion watcher and V2 path retire only `Completed` /
  `Errored`. Broad retirement of `Shutdown` regressed an existing
  resume-path test, so it's left alone — flagged as an open question.
  Whether `handlers::shutdown` actually runs and whether
  `begin_shutdown()` actually fires for the retired session is not
  asserted at the unit level; that needs the integration test in the
  pending follow-up list.
- `get_status`, `wait_agent`, and `list_agents` fall back to cached
  registry status when the live thread is gone. `resume_agent` switches
  to `has_live_thread(thread_id)` instead of treating any non-`NotFound`
  status as proof of non-resumability.
- `handlers::shutdown` does two descendant shutdown passes (before and
  after `conversation.shutdown()`) and clears the spawned-agent registry
  only when `live_thread_spawn_descendants` returns empty. The drain is
  bounded by a hard 30 s wall-clock deadline (each
  `shutdown_live_agent` is wrapped in `tokio::time::timeout_at`, not just
  the outer loop), a 64-sweep cap, and a 50 ms backoff between sweeps
  whose live-set didn't shrink, so root teardown cannot hot-loop or
  block indefinitely on a stuck descendant.
- `SpawnReservation::Drop` releases the reserved nickname if spawn setup
  fails before commit.

Per-file behavioral diff and residual risk:
[`artifacts/pr1-change-dossier.md`](artifacts/pr1-change-dossier.md).

Out of scope: inter-agent mailbox backpressure, per-session MCP ownership
redesign, app-server disconnect cleanup, terminal restore semantics. These are
PR2 follow-up tracks in [`artifacts/pr1-pr2-plan.md`](artifacts/pr1-pr2-plan.md).

## Verification

The canonical-patch verification results (against `openai/codex@80fb0704ee`,
on branch `fix/subagent-retention-after-19753`) are summarized in the
"Patch" section above. This section captures supplementary observations.

### Strict-provenance Miri (run on the original investigation branch)

Run on the `3895ddd6b` patched branch (nightly, macOS arm64):

```bash
MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation' \
  cargo +nightly miri test -p codex-core --lib retire_releases_slot_and_preserves_cached_status
MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation' \
  cargo +nightly miri test -p codex-core --lib failed_spawn_releases_reserved_nickname
```

Both passed. These results have not been re-run on the canonical
`80fb0704ee` re-target — happy to refresh on request. The `cfg(miri)` guards
in `tests/common/lib.rs` and `tests/suite/mod.rs` no-op test-startup helpers
that do `realpath` / `chmod` / `PATH` mutation, which strict-provenance Miri
rejects before reaching the lifecycle code. Tokio handler tests remain
blocked under macOS Miri by unsupported `kqueue` calls — a pre-existing
platform limitation, unrelated to this patch.

### Pre-existing failures observed on the original branch (verified unrelated)

These were observed during the original investigation against `3895ddd6b`,
not on the canonical re-target. They reproduce on a clean upstream clone at
the same commit, so they are unrelated to the patch:

- `suite::realtime_conversation::conversation_webrtc_start_posts_generated_session`
  — fails the same way on clean upstream `3895ddd6b`.
- `suite::subagent_notifications::subagent_notification_is_included_without_wait`
  — passes in isolation on both the patched and clean upstream branches at
  `3895ddd6b`; long-suite interaction.
- `suite::cli_stream::responses_mode_stream_cli_supports_openai_base_url_config_override`
  — passes in isolation on the patched `3895ddd6b` branch.
- `cargo test -p codex-app-server` was red in
  `realtime_webrtc_start_emits_sdp_notification` and
  `webrtc_v1_start_posts_offer_returns_sdp_and_joins_sideband`; both
  reproduced on clean upstream `3895ddd6b`.

These observations are unverified against current `main` and may have been
fixed since.

## Apply

Canonical patch (against current `main` after #19753):

```bash
git clone https://github.com/openai/codex.git
cd codex
git checkout 80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b
git apply /path/to/patches/pr1-subagent-retention-after-19753.patch
cd codex-rs
cargo fmt --all -- --check
cargo clippy -p codex-core --tests -- -D warnings
cargo test -p codex-core --lib retire_releases_slot_and_preserves_cached_status -- --nocapture
cargo test -p codex-core --lib spawn_agent_releases_slot_after_completion -- --nocapture
cargo test -p codex-core --lib root_shutdown_shuts_down_live_spawned_descendants -- --nocapture
cargo test -p codex-core --lib failed_spawn_releases_reserved_nickname -- --nocapture
```

Or, equivalently, fetch the topic branch directly via `git format-patch` /
`git am`:

```bash
git am /path/to/patches/pr1-subagent-retention-after-19753.patch
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
