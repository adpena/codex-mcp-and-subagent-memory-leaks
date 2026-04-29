Thank you for the detailed forensic data here — the 213-pair Playwright leak
breakdown and the `vmmap` analysis make the failure mode much easier to reason
about. I noticed [#19753](https://github.com/openai/codex/pull/19753) was
merged earlier today; this comment is intended to complement it, not to
re-open ground it already covers.

I've spent some time on a root-cause analysis from the `codex-rs/core/` side.
The full writeup, repro harness, and a candidate patch are at
https://github.com/adpena/codex-mcp-and-subagent-memory-leaks. Short version
below; happy to refine or refresh anything if it helps the team.

### Where #19753 lands

#19753 adds `mcp_connection_manager.begin_shutdown()` to
`session::handlers::shutdown` and to the implicit-shutdown path in
`submission_loop`, plus extensive process-group cleanup in
`stdio_server_launcher.rs`. That closes the "MCP servers leak past root
shutdown" surface for the *root session's own* MCP servers.

It does **not** modify `agent/registry.rs` or `agent/control.rs`, so the
spawned-agent registry semantics are unchanged. Two structural gaps remain:

### Two defects #19753 doesn't address

Verified at the source level against
[`openai/codex@80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b):

1. **Spawned-agent slot retention.** `AgentRegistry::release_spawned_thread`
   ([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99))
   only decrements when thread metadata is removed. Its two callers in
   `agent/control.rs`
   ([691](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L691),
   [714](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L714))
   trigger only on `CodexErr::InternalAgentDied` and explicit
   `shutdown_live_agent`. `Completed` / `Errored` finalization (mapped from
   `TurnComplete` / `TurnAborted` in `agent/status.rs`) does **not** retire a
   slot. Finalized children continue to count against `agents.max_threads`
   and continue to own their session resources — including their per-session
   `McpConnectionManager` — until something else triggers removal. Under
   recursive subagent fanout, that doesn't happen until shutdown, which is
   exactly the window where the 213 retained Playwright pairs in this
   report's `vmmap` data are spawned and held.
2. **Live-subagent descendant drain on root shutdown.**
   `session::handlers::shutdown`
   ([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879))
   tears down the root session's MCP / unified-exec / conversation / guardian
   state. After #19753, MCP servers owned by the root session are also
   explicitly shut down. But the root does not call
   `live_thread_spawn_descendants`
   ([`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164)
   — already used elsewhere by `close_agent`) to drain live spawned
   descendants. Subagent threads that became observable mid-shutdown can
   still outlive the root, and once they do their own MCP servers (a
   per-session manager each — see below) survive whatever cleanup the root
   ran.

#19753's PR description notes the per-session ownership shape explicitly: it
fixes the *root session's* MCP teardown via explicit `begin_shutdown`. Under
recursive subagent spawning, the surviving leak surface is the *live
descendant subagents*, each of which carries its own `McpConnectionManager`
([`session/handlers.rs:884`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L884)).
The combination of (1) finalization not retiring slots and (2) shutdown not
draining descendants means descendants reach end-of-turn but are not actually
torn down — and so their MCP servers, gated by their containing session's
shutdown that never fires, persist.

This is consistent with `#16895` reducing but not eliminating the leak in
April, and with this issue's report on `0.120.0` showing 213 pairs after the
fix.

### 30-second verification

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# → 0 matches: no retirement-on-finalization path exists.

git grep -n 'live_thread_spawn_descendants' codex-rs/core/src/session/
# → 0 matches: the descendant-drain primitive isn't called from shutdown.
```

### Cross-platform / not just macOS

The defects are in platform-agnostic Rust code (no `cfg(target_os)` guards on
any of the affected files). That matches the existing reports at
[#16828](https://github.com/openai/codex/issues/16828) (Linux: 49.4 GB peak,
hard-froze a 64 GB CachyOS workstation),
[#12414](https://github.com/openai/codex/issues/12414) (Windows 10: 90 GB
commit growth → system OOM),
[#19381](https://github.com/openai/codex/issues/19381) (Windows app + VS Code
extension: 10 GB+), and
[#18103](https://github.com/openai/codex/issues/18103) (macOS + zellij /
Ghostty, watchdog panic).

### Worst-offender MCP servers in the reporter's experience

Browser-automation stdio MCP servers (`@playwright/mcp`, `chrome-devtools`
MCPs, similar) trigger the failure mode fastest, presumably because each child
session drags a headless browser process tree along with its connection
manager (renderer, GPU, network service). The structural defects above are
not specific to any MCP server vendor — heavyweight servers just amplify the
leak's RSS cost — but browser-automation servers are the most reliable
trigger, which lines up with this report's Playwright-specific data.

### Note on the released CLI

`codex-cli 0.125.0` (release `rust-v0.125.0`, published 2026-04-24) predates
the #19753 merge by four days, so users on the latest released CLI still see
the unfixed shape for *all three* defects, not just the two above.

### Suggested fix outline (complementary to #19753)

This is a high-level sketch in the spirit of `docs/contributing.md`'s
invitation criteria, not a PR. The candidate patch in the repo
([`patches/pr1-subagent-retention-root-teardown.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-root-teardown.patch),
9 files, 410 / 23) was authored against `3895ddd6b` and predates the
`codex.rs` → `session/{handlers.rs, …}` split, so it needs re-targeting onto
current `main` — happy to do that on request.

**Defect #1 — registry-level retirement.** Introduce an explicit "retired
but resumable" state on `AgentMetadata`, decoupling slot accounting from
metadata removal:

- Add `last_status: Option<AgentStatus>` and `slot_active: bool` fields on
  `AgentMetadata`.
- Add `AgentRegistry::retire_spawned_thread(thread_id, status)` that caches
  the final status, flips `slot_active = false`, and decrements
  `total_count` exactly once. Existing `release_spawned_thread` callers
  (`InternalAgentDied`, explicit `shutdown_live_agent`) continue to work
  unchanged.
- Add `AgentControl::retire_finalized_agent(thread_id, status)` invoked from
  the completion watcher when status transitions to `Completed` /
  `Errored`. `Shutdown` is intentionally left out — broad retirement of
  shutdown states regressed an existing resume-path test, so it's flagged as
  an open question rather than included in the change.
- `get_status`, `wait_agent`, and `list_agents` fall back to cached registry
  status when no live thread exists, so retirement doesn't break
  collaboration UX. `resume_agent` switches to `has_live_thread(thread_id)`
  rather than treating any non-`NotFound` status as proof of
  non-resumability.

**Defect #3 (remaining) — live-descendant drain on root shutdown.** The
descendant traversal already exists; it just isn't invoked from
`session::handlers::shutdown`:

- In `session::handlers::shutdown`, after `abort_all_tasks` and before /
  after `conversation.shutdown()`, call
  `agent_control.live_thread_spawn_descendants(root_thread_id)` and submit
  shutdown to each, tolerating `ThreadNotFound` / `InternalAgentDied`.
- Two passes (before and after the conversation shutdown) catch descendants
  that only become observable mid-teardown.
- Only clear the spawned-agent registry state at the end if no live
  descendants remain detectable — avoid optimistic zeroing.

**Hygiene fix:** `SpawnReservation::Drop` releases the reserved nickname if
spawn setup fails before commit, preventing slow nickname-pool poisoning.

**Out of scope (intentionally).** Defect #2 — per-session
`McpConnectionManager` ownership — is the right next investigation but not in
this patch. After defects #1 and #3 are closed, descendants stop existing
past their useful life, which removes most of the per-session-MCP-fanout
RSS cost as a side effect. A proper architectural decision (per-session vs.
shared vs. lazy) deserves a separate proposal.

**Verification.** The original PR1 patch comes with new tests in
`agent/registry_tests.rs` and `agent/control_tests.rs`
(`retire_releases_slot_and_preserves_cached_status`,
`spawn_agent_releases_slot_after_completion`,
`root_shutdown_shuts_down_live_spawned_descendants`,
`failed_spawn_releases_reserved_nickname`) which fail before the change and
pass after. Strict-provenance Miri passes for the registry-level tests on
macOS arm64; Tokio-handler-level Miri remains blocked by `kqueue` in the
runtime — pre-existing platform limitation.

### What's in the repo

- Full per-file rationale and behavioral diff:
  [`artifacts/pr1-change-dossier.md`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/pr1-change-dossier.md)
- Hostile soak harness with deterministic SSE fixtures:
  [`artifacts/soak_codex_concurrency.py`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/soak_codex_concurrency.py)
- Candidate patch (against `3895ddd6b`, predates the
  `codex.rs` → `session/{handlers.rs, …}` split — happy to re-target onto
  current `main`):
  [`patches/pr1-subagent-retention-root-teardown.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-root-teardown.patch)
- Suggested `#[tokio::test]` outlines the team can paste into
  `core/src/agent/control_tests.rs` to deterministically reproduce defects #1
  and #3:
  [`artifacts/upstream-issue-draft.md`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/upstream-issue-draft.md)
- Fresh soak telemetry against `codex-cli 0.125.0`
  ([`artifacts/soak-summaries/current-main-20260428-212049/`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/tree/main/artifacts/soak-summaries/current-main-20260428-212049)):
  the lifecycle log hooks the harness was built around have been removed in
  upstream, and `spawn_agent` is now feature-gated (the SSE fixture's
  `function_call: spawn_agent` events return `unsupported call: spawn_agent` —
  50,292 such rejections in worker-00). The harness needs adaptation to drive
  the gated handler before it can demonstrate defects #1 and #3 behaviorally
  on `0.125.0`. What remains visible: 6 workers spawn ~44 stdio MCP child
  processes during steady state (~7 per worker — defect #2 confirmed), and
  shutdown emits `Failed to terminate MCP process group: No such process`
  warnings indicating a race in the cleanup path.

I read [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md)
before posting. This is offered as analysis material in the spirit of that
policy — I am not opening a PR. If the team would find the candidate patch
useful, I'd be glad to follow the invitation process and re-target it onto
current `main`.

— Alejandro Pena ([@adpena](https://github.com/adpena))
