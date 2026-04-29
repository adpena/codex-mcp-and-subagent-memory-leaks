Thank you for the detailed forensic data here — the 213-pair Playwright
breakdown and the `vmmap` analysis make the failure mode much easier to
reason about. I noticed [#19753](https://github.com/openai/codex/pull/19753)
was merged earlier today (2026-04-28); this comment is intended to
complement it, not to re-open ground it already covers.

I've spent some time on a root-cause analysis from the `codex-rs/core/`
side. The full writeup, repro harness, and a candidate patch are at
https://github.com/adpena/codex-mcp-and-subagent-memory-leaks. Short version
follows.

### Where #19753 lands, and where it doesn't

#19753 adds two specific call sites in `codex-rs/core/src/session/handlers.rs`:

- `pub async fn shutdown(...)`
  ([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879))
  now calls `mcp_connection_manager.begin_shutdown()` at
  [`session/handlers.rs:887`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L887)
  before flushing thread persistence.
- The `submission_loop` exit path (same file, around
  [line 1180](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L1180))
  also calls `terminate_all_processes` + `begin_shutdown` when the channel
  closes without an explicit shutdown op.

It also adds extensive process-group cleanup primitives in
`stdio_server_launcher.rs` (155 insertions) so the actual process-tree kill
is reliable.

Both call sites live in shared session shutdown code, so the fix applies to
**any** session that enters shutdown — root or subagent. Net effect: if a
session reaches one of those two paths, its `McpConnectionManager` is now
torn down deterministically. That cleanly closes the leak surface for
sessions whose lifecycle ends normally.

What it doesn't change is *whether* a session enters shutdown in the first
place. Two structural gaps in `agent/registry.rs` and `agent/control.rs`
(neither of which #19753 touches) can keep a session alive past the point
where it should have entered shutdown. While the session is still alive, its
`McpConnectionManager` is also still alive — and #19753's cleanup never runs
for that session, because the cleanup is gated on shutdown.

### Two structural gaps that remain after #19753

Verified at the source level against
[`openai/codex@80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)
(both `multi_agents` V1 and `multi_agents_v2` go through this same agent
control / registry / shutdown infrastructure, so the gaps apply to both
surfaces):

**Gap A — Spawned-agent slot retention.**
`AgentRegistry::release_spawned_thread`
([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99))
only decrements `total_count` when thread metadata is removed. Its two
callers in `agent/control.rs`
([`693`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L693),
[`714`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L714))
trigger removal only on `CodexErr::InternalAgentDied` and explicit
`shutdown_live_agent`. `Completed` / `Errored` finalization (mapped from
`TurnComplete` / `TurnAborted` in
[`agent/status.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/status.rs))
does **not** retire a slot. A finalized subagent's session stays alive,
counts against `agents.max_threads`, and continues to own its
`McpConnectionManager` — until something else triggers removal. Under
recursive subagent fanout, that "something else" doesn't fire until root
shutdown, which is precisely the window in which the 213 retained Playwright
pairs in this report's `vmmap` data accumulate.

**Gap B — Live-spawned-descendant drain on root shutdown.**
`session::handlers::shutdown`
([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879))
tears down the root session's MCP / unified-exec / conversation / guardian
state. After #19753, the root's MCP servers are also explicitly shut down.
But the root does not call `live_thread_spawn_descendants`
([`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164)
— already used elsewhere by `close_agent`) to walk the live spawn tree and
signal each descendant to shut down. Subagent threads that are live
mid-teardown therefore never enter their own shutdown path — which means
Gap A above plus this gap is what allows subagent `McpConnectionManager`s
to survive the root's exit.

The two gaps compound: descendants reach end-of-turn but aren't retired
(Gap A), and root shutdown doesn't signal them to clean up (Gap B). Their
sessions stay alive, their MCP servers stay alive, and #19753's cleanup
never runs for them. That's consistent with the prior partial fix in
#16895 reducing but not eliminating the leak, and with this report's 213
pairs on `0.120.0`.

### 30-second verification

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# → 0 matches: no retirement-on-finalization path exists in the registry
# or control layer.

git grep -n 'live_thread_spawn_descendants' codex-rs/core/src/session/
# → 0 matches: the descendant-drain primitive (which exists at
# agent/control.rs:1164) isn't called from any session shutdown path.
```

### Cross-platform

The defects sit in platform-agnostic Rust code (no `cfg(target_os)` guards
on any of the affected files). That matches existing reports beyond macOS:
[#16828](https://github.com/openai/codex/issues/16828) (Linux: 49.4 GB peak,
hard-froze a 64 GB CachyOS workstation),
[#12414](https://github.com/openai/codex/issues/12414) (Windows 10: 90 GB
commit growth → system OOM),
[#19381](https://github.com/openai/codex/issues/19381) (Windows app + VS
Code extension: 10 GB+),
[#18103](https://github.com/openai/codex/issues/18103) (macOS + zellij /
Ghostty, watchdog panic).

### Worst-offender MCP servers

Browser-automation stdio MCP servers (`@playwright/mcp`, chrome-devtools
MCPs, similar) trigger the failure mode fastest, presumably because each
child session drags a headless browser process tree along with its
connection manager (renderer, GPU, network service). The structural gaps
above aren't specific to any vendor — heavyweight servers just amplify the
RSS cost — but browser-automation servers are the most reliable trigger,
which lines up with this report's Playwright-specific data.

### Note on the released CLI

`codex-cli 0.125.0` (release `rust-v0.125.0`, published 2026-04-24)
predates the #19753 merge by four days, so users on the latest released CLI
still see the unfixed shape for the leak surface #19753 was intended to
close, in addition to Gaps A and B above.

### Suggested fix outline (complementary to #19753)

A high-level sketch in the spirit of `docs/contributing.md`'s invitation
criteria, not a PR. The candidate patch in the repo
([`patches/pr1-subagent-retention-root-teardown.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-root-teardown.patch),
9 files, 410 insertions / 23 deletions) was authored against `3895ddd6b`
and predates the `codex.rs` → `session/{handlers.rs, …}` split, so it
needs re-targeting onto current `main` (see closing note).

**Gap A — registry-level retirement.** Decouple slot accounting from
metadata removal by introducing an explicit "retired but resumable" state:

- Add `last_status: Option<AgentStatus>` and `slot_active: bool` to
  `AgentMetadata`.
- Add `AgentRegistry::retire_spawned_thread(thread_id, status)` that caches
  the final status, flips `slot_active = false`, and decrements
  `total_count` exactly once. Existing `release_spawned_thread` callers
  (`InternalAgentDied`, `shutdown_live_agent`) keep working unchanged.
- Add `AgentControl::retire_finalized_agent(thread_id, status)`, invoked
  from the completion watcher when status transitions to `Completed` /
  `Errored`. `Shutdown` is intentionally left out — broad retirement of
  shutdown states regressed an existing resume-path test, so it's flagged
  as an open question rather than included in the change.
- `get_status`, `wait_agent`, and `list_agents` fall back to cached
  registry status when no live thread exists, so retirement doesn't break
  collaboration UX. `resume_agent` switches to `has_live_thread(thread_id)`
  rather than treating any non-`NotFound` status as proof of
  non-resumability.

After this change, finalized subagents enter `session::handlers::shutdown`
on retirement → #19753's `mcp_connection_manager.begin_shutdown()` fires →
their MCP servers and stdio children are torn down deterministically. The
two fixes compose cleanly.

**Gap B — live-descendant drain on root shutdown.** The descendant
traversal already exists; integrate it into the root shutdown sequence:

- In `session::handlers::shutdown`, after `abort_all_tasks` and *before*
  `conversation.shutdown()`, call
  `agent_control.live_thread_spawn_descendants(root_thread_id)` and submit
  shutdown to each, tolerating `ThreadNotFound` / `InternalAgentDied`.
  Doing this before the conversation teardown gives each descendant a clean
  shutdown path while the parent context is still intact.
- After `mcp_connection_manager.begin_shutdown()` (the call #19753 added),
  do a second descendant pass to catch any descendant that only became
  observable mid-teardown.
- Only clear the spawned-agent registry state at the end if no live
  descendants remain detectable — avoid optimistic zeroing that loses
  in-flight state.

**Hygiene fix.** `SpawnReservation::Drop` releases the reserved nickname
when spawn setup fails before commit, preventing slow nickname-pool
poisoning over long sessions.

**Out of scope (intentionally), with reasoning.** A third surface — the
architectural question of `McpConnectionManager` ownership (per-session as
today vs. shared per-process vs. lazy on first tool use vs. pooled with
config-fingerprint keys) — is left alone here for two reasons:

1. *It's a design decision, not a bug fix.* Each shape has different
   isolation, restart-on-config-change, and resource-cost tradeoffs.
   Picking among them deserves its own proposal where the tradeoffs can
   be evaluated without being conflated with the straightforward bug
   fixes above.
2. *After Gaps A and B are closed, the symptom this would mitigate is
   largely resolved as a side effect.* Finalized subagents will then enter
   `session::handlers::shutdown`, which (post-#19753) tears down their
   `McpConnectionManager` and its stdio children. The per-session
   ownership shape stays the same, but per-session MCP fanout no longer
   accumulates because each subagent's MCP servers are reaped at the
   right point in the lifecycle. Whether to revisit ownership is then a
   refinement, not a critical fix.

I can write the architectural-options analysis as a separate proposal if
useful.

**Verification.** The original PR1 patch ships with new tests in
`agent/registry_tests.rs` and `agent/control_tests.rs` —
`retire_releases_slot_and_preserves_cached_status`,
`spawn_agent_releases_slot_after_completion`,
`root_shutdown_shuts_down_live_spawned_descendants`,
`failed_spawn_releases_reserved_nickname` — which fail before the change
and pass after. Strict-provenance Miri passes for the registry-level tests
on macOS arm64; Tokio-handler-level Miri remains blocked by `kqueue` in
the runtime, which is a pre-existing platform limitation unrelated to this
work.

### What's in the repo

- Full per-file rationale and behavioral diff:
  [`artifacts/pr1-change-dossier.md`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/pr1-change-dossier.md).
- Hostile soak harness with deterministic SSE fixtures:
  [`artifacts/soak_codex_concurrency.py`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/soak_codex_concurrency.py).
  A refresh against `codex-cli 0.125.0` is in
  [`artifacts/soak-summaries/current-main-20260428-212049/`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/tree/main/artifacts/soak-summaries/current-main-20260428-212049).
  Caveats: the lifecycle log strings the harness greps for
  (`reserved spawned-agent slot`, etc.) were diagnostic instrumentation
  in the original investigation build and were never part of upstream
  `openai/codex`, so the harness's lifecycle counters report `0` against
  unmodified upstream binaries; `spawn_agent` registration also changed
  enough that the SSE fixture's tool-call shape is rejected as
  `unsupported call: spawn_agent` on `0.125.0`. What remains visible
  unmodified: 6 workers spawn ~44 stdio MCP child processes during steady
  state (~7 per worker), demonstrating that even without recursive
  subagent driving, the per-session-MCP-manager pattern is observable.
- Candidate patch (against `3895ddd6b`):
  [`patches/pr1-subagent-retention-root-teardown.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-root-teardown.patch).

I read [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md)
before posting. This is offered as analysis material in the spirit of that
policy — not as an unsolicited PR. If the team finds the candidate patch
useful, I'll re-target it onto current `main` and follow the invitation
process.

— Alejandro Pena ([@adpena](https://github.com/adpena))
