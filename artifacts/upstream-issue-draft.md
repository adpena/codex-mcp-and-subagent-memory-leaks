# Draft: openai/codex CLI bug report

This is the draft body for an issue at `openai/codex`, matching the
[`4-cli.yml`](https://github.com/openai/codex/blob/main/.github/ISSUE_TEMPLATE/3-cli.yml)
template field-by-field. It follows the by-invitation contribution policy in
[`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md):
this is an analysis report, not an unsolicited PR. The repo at
https://github.com/adpena/codex-mcp-and-subagent-memory-leaks contains the full
investigation, soak harness, and a candidate patch in case the team finds the
direction useful and chooses to invite a PR.

The fields below map 1:1 onto the issue form. **`[FILL IN]` markers must be
replaced with concrete values before submission.**

---

## Title

> Subagent slot retention and per-session MCP fanout cause cumulative degradation in long, concurrent sessions on macOS

## What version of Codex CLI is running?

`codex-cli 0.125.0`

(Behavior also verified at the source level against
[`openai/codex@80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b)
— current `main` at the time of this report, 685 commits ahead of the original
investigation point `3895ddd6b`. The structural defects below are still present
in current `main`.)

## What subscription do you have?

`[FILL IN — your Codex subscription tier, e.g. ChatGPT Plus / Pro / Enterprise / API]`

## Which model were you using?

`[FILL IN — e.g., gpt-5.2-codex]`

## What platform is your computer?

`Darwin 25.4.0 arm64 arm` (macOS 26.4, Apple Silicon, high-memory machine)

## What terminal emulator and version are you using?

Ghostty 1.3.1, with several panes open at once. No terminal multiplexer (no
tmux / screen / zellij).

## What issue are you seeing?

Under sustained, highly concurrent usage on macOS, a Codex CLI session degrades
cumulatively until it becomes effectively unusable. Symptoms (in roughly the
order they appear):

- the pane with the most subagents and the heaviest MCP traffic begins to
  flicker, jank, and mis-render
- `memallocstack`-style failures eventually surface
- after the final hard failure, `Ctrl+C` may stop responding in the affected
  pane
- if recovery is attempted from a neighboring pane, agents can begin
  interfering with each other's processes, propagating the damage

Source-level analysis against current `main`
([`openai/codex@80fb0704ee`](https://github.com/openai/codex/commit/80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b))
points to three structural causes that compound under recursive subagents +
stdio MCP fanout:

1. **Spawned-agent slot retention.** `AgentRegistry::release_spawned_thread`
   ([`codex-rs/core/src/agent/registry.rs:99`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/registry.rs#L99))
   only decrements `total_count` when thread metadata is removed from
   `agent_tree`. The two callers in `agent/control.rs` trigger removal only on:

   - `CodexErr::InternalAgentDied`
     ([`control.rs:691`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L691))
   - explicit `shutdown_live_agent`
     ([`control.rs:714`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L714))

   `Completed` / `Errored` finalization (mapped from `TurnComplete` /
   `TurnAborted` in
   [`agent/status.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/status.rs))
   does **not** trigger slot release. Finalized children continue to count
   against `agents.max_threads`.

2. **Per-session MCP fanout.** Each session's `SessionServices` owns its own
   `McpConnectionManager`
   ([`session/handlers.rs:884`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L884)).
   Recursive subagent spawning therefore multiplies stdio MCP child processes.
   In the soak baseline below, one worker produced 7 MCP child processes from
   6 reserved slots.

3. **Single-pass root-session teardown.** The current `shutdown` handler
   ([`session/handlers.rs:879`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/session/handlers.rs#L879))
   aborts tasks, shuts down the conversation / unified-exec / MCP /
   guardian-review, and flushes thread persistence — but does not call
   `live_thread_spawn_descendants` (which already exists at
   [`agent/control.rs:1164`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/src/agent/control.rs#L1164)).
   Under recursive fanout, descendants that become observable mid-shutdown can
   outlive the root.

The combination, not any one alone, makes the busiest root session the first
to destabilize. That matches the user-visible failure mode: "the pane with the
most subagents always crashes first."

## What steps can reproduce the bug?

The pattern that produces this in real use:

- macOS, Apple Silicon, high-memory machine
- Ghostty with several panes open simultaneously
- each pane running an interactive `codex` session
- recursive subagent spawning during normal work
- a busy MCP configuration including stdio servers whose tools occasionally
  hang or fail without returning cleanly
- subagents frequently started but not explicitly closed before the root
  session is interrupted or restarted
- sessions kept alive for hours

A focused soak harness that exercises the same code paths is at
https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/artifacts/soak_codex_concurrency.py.
On the `spawn_recursive` scenario against the original investigation commit
[`3895ddd6b`](https://github.com/openai/codex/commit/3895ddd6b1caf80cd77d6fd44e3ce55bd290ef18),
one worker produced:

| Counter             | Value |
| ------------------- | ----- |
| `agent_slot_reserved` | 6     |
| `agent_slot_released` | **0** |
| `mcp_spawned`         | 7     |
| `mcp_dropped`         | 7     |

(The `CODEX_DEBUG_AGENT_LIFECYCLE` / `CODEX_DEBUG_MCP_LIFECYCLE` /
`CODEX_DEBUG_THREAD_LISTENERS` log hooks the harness depended on appear to have
been removed in the meantime; I'm happy to update the harness or add a small
diagnostic trace event behind a feature flag if the team would find current
telemetry useful.)

## What is the expected behavior?

Three properties that I believe the code is intended to satisfy:

1. Finalized spawned agents (`Completed` / `Errored`) release their registry
   slot — they should not continue to count against `agents.max_threads` after
   they are effectively done. Cached final status should remain visible to
   `wait_agent`, `list_agents`, and `resume_agent` so collaboration UX is
   preserved.
2. Root-session shutdown drains live spawned descendants. The existing
   `live_thread_spawn_descendants` traversal at `agent/control.rs:1164` looks
   like the intended primitive; calling it from `session::handlers::shutdown`
   would close the gap.
3. Failed pre-commit spawn setup releases the reserved nickname so the
   nickname pool does not accumulate poisoned entries over long sessions.

## Additional information

A full writeup with per-file rationale, residual risk analysis, soak harness,
and a candidate patch is at:

**https://github.com/adpena/codex-mcp-and-subagent-memory-leaks**

A few notes for the team:

- I read [`docs/contributing.md`](https://github.com/openai/codex/blob/main/docs/contributing.md)
  and the [`pull_request_template.md`](https://github.com/openai/codex/blob/main/.github/pull_request_template.md)
  before filing this. The repo is offered as analysis, repro details, and a
  high-level outline of a potential fix — exactly the kind of material the
  contributing guide invites. I am not opening a PR; if the team would find a
  PR useful at any point, I would be glad to follow the invitation process.
- The candidate patch in the repo
  ([`patches/pr1-subagent-retention-root-teardown.patch`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/patches/pr1-subagent-retention-root-teardown.patch))
  was authored against `3895ddd6b` and predates the
  `codex.rs` → `session/{handlers.rs, …}` split. The structural defects are
  unchanged, so the diff anchors need re-targeting; I'm happy to provide an
  updated patch on request.
- Recent authorship in the touched subsystems: cc
  [@jif-oai](https://github.com/jif-oai) (primary author of
  `agent/control.rs` and `multi_agents/`),
  [@pakrym-oai](https://github.com/pakrym-oai) (primary author of the file
  formerly known as `codex.rs`, now split into `session/`),
  [@bolinfest](https://github.com/bolinfest) (broad ownership across
  `codex-rs/core/`), and [@tibo-openai](https://github.com/tibo-openai)
  (author of the adjacent
  [#17749 "drain mailbox only at request boundaries"](https://github.com/openai/codex/pull/17749)).
  CODEOWNERS team for `codex-rs/core/`: `@openai/codex-core-agent-team`.
- Thank you for the time. I appreciate the team's clarity on the
  invitation-only model and the reasoning behind it; I tried to put together
  the kind of artifact `contributing.md` describes as most useful.

---

## Submission command (after replacing `[FILL IN]` placeholders above)

```bash
gh issue create \
  --repo openai/codex \
  --title "Subagent slot retention and per-session MCP fanout cause cumulative degradation in long, concurrent sessions on macOS" \
  --label bug \
  --body-file artifacts/upstream-issue-draft-body.md
```

(`upstream-issue-draft-body.md` — a clean version with all `[FILL IN]` blanks
filled and the title section stripped — should be generated from this draft
before running `gh issue create`.)
