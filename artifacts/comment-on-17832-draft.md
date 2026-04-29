# Draft: comment on openai/codex#17832

This is the draft body for a comment on
[openai/codex#17832](https://github.com/openai/codex/issues/17832) — currently
the most concrete active thread on the subagent / MCP teardown leak. The repo
at https://github.com/adpena/codex-mcp-and-subagent-memory-leaks is the durable
artifact; this comment is the targeted handoff into the existing discussion.

The comment below is intended to address jroth1111's request on
[#18103](https://github.com/openai/codex/issues/18103) — "I'd want confirmation
that the root cause has been identified and a fix is in progress" — by giving
the team a concrete, source-level root-cause hypothesis rather than another
symptom report.

---

## Comment body

Thank you for the detailed forensic data on this — the 213-pair Playwright leak
breakdown and the `vmmap` analysis make the failure mode much easier to reason
about.

I've spent some time on a root-cause analysis from the
`codex-rs/core/` side. The full writeup, repro harness, and a candidate patch
are at https://github.com/adpena/codex-mcp-and-subagent-memory-leaks. Short
version below; happy to refine or refresh anything if it helps the team.

### Three structural defects on current `main`

Verified against
[`openai/codex@80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)
(and repeats verbatim on `codex-cli 0.125.0` source):

1. **Spawned-agent slot retention.** `AgentRegistry::release_spawned_thread`
   ([`registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99))
   only decrements when thread metadata is removed. Its two callers in
   `agent/control.rs`
   ([691](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L691),
   [714](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L714))
   trigger only on `CodexErr::InternalAgentDied` and explicit
   `shutdown_live_agent`. `Completed` / `Errored` finalization (mapped from
   `TurnComplete` / `TurnAborted` in `agent/status.rs`) does **not** retire a
   slot. Finalized children continue to count against `agents.max_threads` and
   continue to own their session resources until something else triggers
   removal — which under recursive subagent fanout often doesn't happen until
   shutdown.
2. **Per-session `McpConnectionManager` ownership.** Each session's
   `SessionServices` owns its own `McpConnectionManager`
   ([`session/handlers.rs:884`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L884)),
   so recursive spawning multiplies stdio MCP child processes. This is the
   mechanism behind the 213-pair accumulation here — every retained child
   session keeps its own `npm exec @playwright/mcp@latest` + `node
   playwright-mcp` pair alive, gated by defect #1 above.
3. **Single-pass root-session teardown.** `session::handlers::shutdown`
   ([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879))
   aborts tasks, shuts down the conversation / unified-exec / MCP /
   guardian-review, and flushes thread persistence — but does not call
   `live_thread_spawn_descendants` (which already exists at
   [`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164)
   and is used elsewhere by `close_agent`). Descendants that become observable
   mid-shutdown can outlive the root, especially under heavy recursive fanout.

The combination, not any one alone, explains why the busiest root session is
the first to destabilize and why the prior fix in
[#16895](https://github.com/openai/codex/pull/16895) reduced but didn't
eliminate the leak — it tightened parts of the cleanup path without closing
the finalization-vs-removal gap.

### 30-second verification

```bash
git grep -nE 'retire|slot_active|last_status|cached_status' codex-rs/core/src/agent/
# → 0 matches: no retirement-on-finalization path exists.

git grep -n 'live_thread_spawn_descendants' codex-rs/core/src/session/
# → 0 matches: the descendant-drain primitive isn't called from shutdown.
```

### Cross-platform / not just macOS

The defects are in platform-agnostic Rust code (no `cfg(target_os)` guards on
any of the seven affected files). That matches the existing reports at
[#16828](https://github.com/openai/codex/issues/16828) (Linux: 49.4 GB peak,
hard-froze a 64 GB CachyOS workstation),
[#12414](https://github.com/openai/codex/issues/12414) (Windows 10: 90 GB
commit growth → system OOM), and
[#19381](https://github.com/openai/codex/issues/19381) (Windows + VS Code
extension: 10 GB+).

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

---

## Submission command

```bash
gh issue comment 17832 \
  --repo openai/codex \
  --body-file artifacts/comment-on-17832-body.md
```

(`comment-on-17832-body.md` should be a clean copy of just the comment body —
without the wrapper instructions and `## Comment body` heading above.)
