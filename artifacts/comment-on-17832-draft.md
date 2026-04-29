# Draft: comment on openai/codex#17832

This is the draft body for a comment on
[openai/codex#17832](https://github.com/openai/codex/issues/17832). The repo
at https://github.com/adpena/codex-mcp-and-subagent-memory-leaks is the
durable artifact; this comment is the targeted handoff into the existing
discussion.

The comment was tightened after an adversarial review flagged the prior
draft as too broad. Architecture / cross-platform / soak-data observations
are still in the repo, but kept out of the comment so the comment focuses
on a single defect with concrete evidence.

---

## Comment body

Thank you for the forensic data here — the 213-pair Playwright breakdown
plus the `vmmap` analysis make this much easier to reason about. I noticed
[#19753](https://github.com/openai/codex/pull/19753) was merged on
2026-04-28; this comment is intended to complement it, not to re-open
ground it already covers.

I worked through a root-cause analysis from the `codex-rs/core/` side. Full
writeup, repro harness, candidate patch, and a topic branch with verified
tests are at https://github.com/adpena/codex-mcp-and-subagent-memory-leaks.
Short version follows.

### What #19753 closes, and what it doesn't

#19753 adds `mcp_connection_manager.begin_shutdown()` to
`session::handlers::shutdown` (line 887) and to the `submission_loop` exit
path (around line 1180). Net effect: any session that enters shutdown gets
its `McpConnectionManager` torn down deterministically.

What it doesn't change is *whether* a session enters shutdown. A finalized
spawned subagent (`Completed` / `Errored`) doesn't enter shutdown by
itself — and that's the gap that lets the 213 retained Playwright pairs
accumulate.

### The remaining gap

`AgentRegistry::release_spawned_thread`
([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99))
only decrements `total_count` when thread metadata is removed. Its two
callers in `agent/control.rs`
([`693`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L693),
[`714`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L714))
trigger only on `CodexErr::InternalAgentDied` and explicit
`shutdown_live_agent`. `Completed` / `Errored` finalization (mapped from
`TurnComplete` / `TurnAborted` in
[`agent/status.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/status.rs))
does **not** retire a slot. A finalized subagent's session stays alive,
counts against `agents.max_threads`, and continues to own its
`McpConnectionManager` — until something else triggers removal.

Two source-level checks that confirm this on current `main`:

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# → 0 matches: no retirement-on-finalization path exists.

git grep -n 'live_thread_spawn_descendants' codex-rs/core/src/session/
# → 0 matches: even root shutdown doesn't drive the descendant tree.
```

### Suggested fix outline

The candidate patch on
[fix/subagent-retention-after-19753](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-after-19753.patch)
(against `openai/codex@80fb0704ee`) is in the spirit of `contributing.md`'s
invitation criteria, not an unsolicited PR. Sketch:

- **Registry-level retirement.** Replace `slot_active: bool` +
  `last_status: Option<AgentStatus>` with an explicit
  `enum SlotState { NotTracked, Active, Retired { last_status } }` on
  `AgentMetadata`. Add `AgentRegistry::retire_spawned_thread(thread_id, status)`
  that transitions `Active -> Retired { .. }` and decrements `total_count`
  exactly once.
- **Control-level retirement, on both V1 and V2.** Add
  `AgentControl::retire_finalized_agent`, called from the V1 completion
  watcher (existing) **and** from V2's
  `Session::maybe_notify_parent_of_terminal_turn`
  ([`session/mod.rs:1474`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/mod.rs#L1474)).
  V2 uses different completion plumbing, so without explicit wiring there
  the V1 watcher's gating (`!Feature::MultiAgentV2`) leaves V2 uncovered.
  Retirement also sends `Op::Shutdown` so the session enters its own
  shutdown path — that's what makes #19753's `begin_shutdown` actually
  fire. Without the explicit `Op::Shutdown`, retirement would rely on
  dropping the last `Arc<CodexThread>` to close the channel; that's
  plausible but not deterministic when other callers still hold references.
- **Live-descendant drain on root shutdown.** `live_thread_spawn_descendants`
  ([`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164))
  is already used by `close_agent`. Wire it into `session::handlers::shutdown`
  with a deadline-bounded sweep (30 s wall clock + 64-sweep cap) so
  descendants get a clean shutdown path before the parent context tears down.
- **Hygiene.** `SpawnReservation::Drop` releases the reserved nickname
  when spawn setup fails before commit, preventing slow nickname-pool
  poisoning over long sessions.
- **`Shutdown` is intentionally not retired.** Broad retirement of
  `Shutdown` regressed an existing resume-path test, so the patch leaves
  it alone and flags it as an open question.

### Verification on the candidate patch

Run on the
[fix/subagent-retention-after-19753](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/tree/main/codex)
branch (against `openai/codex@80fb0704ee`):

- `cargo fmt --all -- --check` clean.
- `cargo clippy -p codex-core --tests -- -D warnings` clean.
- Unit/control-level tests pass (V1 + V2):
  `retire_releases_slot_and_preserves_cached_status`,
  `spawn_agent_releases_slot_after_completion`,
  `v2_spawn_agent_releases_slot_after_completion`,
  `root_shutdown_shuts_down_live_spawned_descendants`,
  `failed_spawn_releases_reserved_nickname`.

What the verification doesn't yet cover, and which I'd add on request:

- An integration test along the lines of #19753's
  `process_group_cleanup.rs` that spawns a real `test_stdio_server` MCP
  child, drives the subagent to `Completed`, and asserts the child PID
  is gone after retirement → `Op::Shutdown` → #19753's `begin_shutdown`
  fires. The unit tests prove the registry transitions and the V1+V2
  retirement wiring; an end-to-end MCP-process test would harden the
  "retirement → MCP teardown" claim deterministically rather than
  through code-path reasoning.
- Race tests for the descendant-drain sequencing under concurrent parent
  + grandparent shutdown of the same descendant, and behavior when
  `live_thread_spawn_descendants` errors mid-shutdown.

### What's explicitly out of scope

- The architectural question of `McpConnectionManager` ownership
  (per-session as today vs. shared / lazy / pooled). The defect above is
  a lifecycle-retention bug, not a fanout-design bug; per-session
  ownership still produces fanout while subagents are *live*, and
  retirement only shortens that tail. If the team wants to revisit
  ownership, that deserves its own proposal where the tradeoffs can be
  weighed independently.
- `Shutdown` participation in retirement, as noted above.

### Note on the released CLI

`codex-cli 0.125.0` (`rust-v0.125.0`, published 2026-04-24) predates
#19753 by four days, so users on the latest released CLI still see the
unfixed shape for both #19753's surface and the gap above.

I read [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md)
before posting. This is offered as analysis material in the spirit of that
policy — not as an unsolicited PR. If the team finds the candidate patch
useful, I'll follow the invitation process and add the integration /
race tests above.

— Alejandro Pena ([@adpena](https://github.com/adpena))

---

## Submission command

```bash
gh issue comment 17832 \
  --repo openai/codex \
  --body-file artifacts/comment-on-17832-body.md
```
