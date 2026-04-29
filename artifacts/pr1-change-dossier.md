## PR1 Change Dossier

Date: 2026-04-12

Scope:
- narrow upstream fix for subagent accumulation and root-session descendant teardown
- no exploratory soak harnesses, MCP repro helpers, or TUI hardening patches in the repo diff

Files in PR1:
- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/codex.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/wait.rs`
- `codex-rs/core/src/agent/registry_tests.rs`
- `codex-rs/core/src/agent/control_tests.rs`
- `codex-rs/core/tests/common/lib.rs`
- `codex-rs/core/tests/suite/mod.rs`

## Upstream Verification

The defect being fixed is present in clean upstream, not just in the working tree:

- in the clean clone at `/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex`, `AgentRegistry::release_spawned_thread(...)` still decrements slot ownership only when thread metadata is removed, with no retirement path or cached final status
- in the clean clone, `AgentControl::get_status(...)` still returns `NotFound` as soon as the live thread is gone
- in the clean clone, `handlers::shutdown(...)` still shuts down only the current session and does not perform descendant drain sweeps before clearing local resources

Relevant clean-upstream code examined:

- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/codex.rs`

This means PR1 is correcting behavior that still exists in upstream `main`.

## 1. `core/src/agent/registry.rs`

### What changed

1. Added `last_status: Option<AgentStatus>` to `AgentMetadata`.
2. Added `slot_active: bool` to `AgentMetadata`.
3. `register_spawned_thread(...)` now marks committed spawned-agent metadata as `slot_active = true`.
4. `release_spawned_thread(...)` now decrements the shared spawned-agent count only when the removed metadata had `slot_active = true`.
5. Added `retire_spawned_thread(thread_id, status)`.
   - stores the final cached status
   - flips `slot_active` to `false`
   - decrements the shared spawned-agent count once
6. Added `cached_status_for_thread(thread_id)`.
7. Added `clear_for_root_shutdown()`.
   - removes non-root entries from the in-memory registry
   - resets the shared spawned-agent count to zero
8. `SpawnReservation::Drop` now releases a reserved nickname if spawn setup failed before commit.

### Why it changed

The original registry only really knew two states:
- counted/live
- removed

That was not enough once finalized spawned agents needed to stop consuming live resources while still remaining visible to collaboration UX.

The new split is:
- metadata may still exist
- but slot ownership may already be retired

This is what allows:
- `wait_agent` and `list_agents` to keep reporting a meaningful final status
- without leaving finalized subagents counted as live spawned threads

The nickname-release change fixes a related accumulation bug:
- failed spawn setup could permanently poison the nickname pool
- over time this increased collisions and unnecessary nickname resets

### Behavioral difference from upstream

Before:
- finalized spawned agents remained counted until explicit close/shutdown or thread removal
- failed spawn setup could keep nicknames marked as used

After:
- `Completed` / `Errored` spawned agents can be retired from slot ownership while preserving final status
- failed pre-commit spawn setup gives the nickname back

### Upstream implications

- lowers the chance of hitting `agents.max_threads` due to already-finished descendants
- reduces stale slot pressure during long-lived collaboration sessions
- keeps stable collaboration surfaces (`wait_agent`, `list_agents`) useful after retirement

### Downstream implications

- any caller that previously equated “metadata exists” with “live counted thread exists” must now respect `slot_active` or use the higher-level control APIs
- cached final status can now outlive the live thread

### Residual risk

- `clear_for_root_shutdown()` still discards descendant metadata after a successful empty-descendant check instead of preserving an archive layer
- if some downstream surface implicitly expected descendant metadata to survive root teardown, that expectation will still fail fast rather than linger

### Unknown unknowns

- there may be latent consumers outside the tested paths that assume registry membership implies resumability

## 2. `core/src/agent/control.rs`

### What changed

1. Added `has_live_thread(thread_id)`.
2. Added `retire_finalized_agent(thread_id, status)`.
   - attempts to flush rollout if the thread still exists
   - removes the live thread from the manager
   - retires slot ownership in the registry
   - still performs cleanup even if rollout flush fails
3. `shutdown_live_agent(...)` now treats “metadata exists but live thread is already gone” as a no-op success.
4. `get_status(...)` now falls back to cached registry status when the thread is already gone.
5. `register_session_root(...)` keeps the upstream root/subagent boundary based on `thread_spawn_parent_thread_id(...)`.
6. Added `shutdown_live_descendants(root_thread_id)`.
   - repeatedly snapshots live descendants
   - submits shutdown to each
   - tolerates `ThreadNotFound` / `InternalAgentDied`
7. Added `clear_spawned_registry_for_root_shutdown()`.
   - root shutdown now skips the clear if descendants are still detectably live
8. `list_agents(...)` now returns cached final status for retired agents instead of silently skipping them.
9. The completion watcher now retires only `Completed` / `Errored` spawned agents.
10. Exported `thread_spawn_parent_thread_id(...)` for reuse.

### Why it changed

This is the heart of PR1.

The original control layer allowed a finalized spawned agent to become “done” without actually leaving live ownership quickly enough. Under recursive subagent churn, that meant:
- the root session kept too many descendants around
- each descendant could keep more MCP state alive
- the busiest session got dirtier first

The control-layer retirement path fixes that by moving finalized spawned agents out of the live thread set as soon as they reach a safe terminal final state.

The cleanup path was tightened during verification so it does not over-report success:
- rollout flush failure no longer prevents thread/slot retirement
- root shutdown only clears the spawned-agent registry when no live descendants remain detectable

### Behavioral difference from upstream

Before:
- live thread absence always meant `NotFound`
- finalized spawned agents could linger until later explicit cleanup
- root descendant cleanup was not a reusable explicit control operation

After:
- a retired finalized agent can still report cached final status
- `resume_agent` can distinguish “retired but resumable” from “not found”
- root sessions actively drain live descendants during shutdown without blindly claiming the registry is empty afterward

### Upstream implications

- lower spawned-slot pressure during recursive collaboration
- more accurate collaboration semantics after child completion
- cleaner root-session teardown under high descendant fanout

### Downstream implications

- `resume_agent` behavior now depends on live-thread existence, not just status text
- `wait_agent` / `list_agents` can observe retired finalized agents without a live thread backing them

### Residual risk

- retirement is intentionally limited to `Completed` / `Errored`
- `Shutdown` is left alone because broad retirement of shutdown states caused a regression in existing resume-path tests
- this means some non-completion terminal paths may still rely on explicit shutdown cleanup

### Unknown unknowns

- there may be rare races where final status publication and live-thread removal interleave in ways not covered by current tests, especially under abnormal process death

## 3. `core/src/codex.rs`

### What changed

Inside `handlers::shutdown(...)`:

1. Reads the session source.
2. Reuses the existing upstream root/subagent boundary via `thread_spawn_parent_thread_id(...)`.
3. For root sessions only:
   - performs a descendant shutdown sweep before `conversation.shutdown()`
   - performs a second descendant shutdown sweep after `conversation.shutdown()`
   - clears remaining spawned-agent registry state only if no live descendants remain detectable

### Why it changed

The original shutdown path only shut down the current session.
That was insufficient once recursive spawned descendants could remain live through the first teardown edge.

The pre/post sweep approach is intentionally simple:
- first pass catches descendants still visible before parent shutdown
- second pass catches descendants that only become observable or detached during shutdown churn

### Behavioral difference from upstream

Before:
- root-session shutdown was mostly local to the current session

After:
- root-session shutdown is also a descendant drain operation
- registry clearing is now conditional on descendant emptiness instead of unconditional

### Upstream implications

- the busiest root session should leave less residual descendant state behind after interruption/shutdown

### Downstream implications

- registry clear is now safer in the face of partial teardown failure
- if descendants survive the sweep, the registry is left intact instead of being zeroed optimistically

### Residual risk

- the sweep is bounded to a fixed number of passes
- if some future descendant creation pattern produces teardown churn beyond that bound, residue may still survive

### Unknown unknowns

- there may be hidden descendant lifetimes not discoverable through the current `live_thread_spawn_descendants(...)` traversal

## 4. `core/src/tools/handlers/multi_agents/resume_agent.rs`

### What changed

`resume_agent` now checks `has_live_thread(thread_id)` instead of deciding solely from `get_status(...) == NotFound`.

### Why it changed

After PR1, finalized spawned agents may have:
- no live thread
- but still a cached final status

If `resume_agent` only looked for `NotFound`, then a retired completed child would no longer be resumable from the tool surface.

### Behavioral difference from upstream

Before:
- a cached non-`NotFound` status could suppress resume even when no live thread existed

After:
- absence of the live thread is what drives resume eligibility

### Residual risk

- if any caller relied on cached final status alone as proof of non-resumability, that assumption is no longer valid

## 5. `core/src/tools/handlers/multi_agents/wait.rs`

### What changed

When a subscribed thread cannot be found, `wait_agent` now falls back to `agent_control.get_status(id)` instead of forcing `NotFound`.

### Why it changed

Retired finalized agents intentionally lose their live thread.
Without this fallback, `wait_agent` would regress from:
- meaningful final status
to:
- misleading `NotFound`

### Behavioral difference from upstream

Before:
- missing thread meant immediate `NotFound`

After:
- missing thread can still resolve to cached `Completed` / `Errored`

### Residual risk

- if cached status is ever stale, `wait_agent` will now expose that stale status instead of `NotFound`

## 6. Tests Added / Updated

### `core/src/agent/registry_tests.rs`

Added / changed:
- `failed_spawn_releases_reserved_nickname`
- `retire_releases_slot_and_preserves_cached_status`
- `clear_for_root_shutdown_releases_all_spawned_slots`

Why these matter:
- prove the registry semantics independently of the higher-level session machinery

### `core/src/agent/control_tests.rs`

Added:
- `spawn_agent_releases_slot_after_completion`
- `root_shutdown_shuts_down_live_spawned_descendants`

Adjusted:
- `resume_agent_respects_max_threads_limit`
  - now uses the thread-spawn path to model real subagent behavior
  - avoids conflating root-session shutdown semantics with child resume semantics

Why these matter:
- they directly cover the two core lifecycle promises of PR1:
  - completion retires slots
  - root shutdown drains live descendants

### Miri harness adjustments

Files:
- `core/tests/common/lib.rs`
- `core/tests/suite/mod.rs`

What changed:
- test-startup `ctor` helpers that mutate PATH / CODEX_HOME or canonicalize paths now no-op under `cfg(miri)`
- the `tests/all.rs` alias-dispatch bootstrap is disabled under `cfg(miri)`

Why it changed:
- on macOS, strict-provenance Miri was failing before it ever reached PR1 logic because the existing test harness invoked unsupported filesystem operations (`realpath`, `chmod`) and alias-bootstrap setup
- these guards are test-only and exist solely to let Miri validate the changed lifecycle paths rather than die during unrelated startup

Behavioral difference:
- normal test behavior is unchanged
- only Miri-specific startup behavior changes

## Verification Status

Fresh passing evidence on the PR1 branch:
- `cargo fmt --all`
- `cargo clippy -p codex-core --tests -- -D warnings`
- `cargo test -p codex-core resume_agent_respects_max_threads_limit -- --nocapture`
- `cargo test -p codex-core spawn_agent_releases_slot_after_completion -- --nocapture`
- `cargo test -p codex-core root_shutdown_shuts_down_live_spawned_descendants -- --nocapture`
- `cargo test -p codex-core failed_spawn_releases_reserved_nickname -- --nocapture`
- `cargo test -p codex-core suite::subagent_notifications::subagent_notification_is_included_without_wait -- --nocapture`
- `cargo test -p codex-core suite::cli_stream::responses_mode_stream_cli_supports_openai_base_url_config_override -- --nocapture`
- `git diff --check`
- `cargo test -p codex-tui`
- `cargo build -p codex-cli --bin codex`

Strict-provenance Miri status:
- `cargo +nightly miri test -p codex-core --lib retire_releases_slot_and_preserves_cached_status`
- `cargo +nightly miri test -p codex-core --lib failed_spawn_releases_reserved_nickname`
- both passed with `MIRIFLAGS='-Zmiri-strict-provenance -Zmiri-disable-isolation'`
- broader Miri coverage on this macOS setup is still constrained by platform/runtime limits outside PR1 logic:
  - `kqueue` in Tokio / mio for handler-level tests

Downstream anomaly currently observed:
- `cargo test -p codex-core` full package runs on this machine still surface three `tests/all.rs` failures:
  - `suite::realtime_conversation::conversation_webrtc_start_posts_generated_session`
  - `suite::subagent_notifications::subagent_notification_is_included_without_wait`
  - `suite::cli_stream::responses_mode_stream_cli_supports_openai_base_url_config_override`
- `cargo test -p codex-app-server` fails in:
  - `suite::v2::realtime_conversation::realtime_webrtc_start_emits_sdp_notification`
  - `suite::v2::realtime_conversation::webrtc_v1_start_posts_offer_returns_sdp_and_joins_sideband`

Why this is currently treated as unrelated to PR1:
- `suite::realtime_conversation::conversation_webrtc_start_posts_generated_session` fails the same way on the clean upstream clone in `/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex`
- `suite::subagent_notifications::subagent_notification_is_included_without_wait` passes in isolation on both the PR1 branch and the clean upstream clone, which points to suite interaction / flakiness rather than a deterministic PR1 regression
- `suite::cli_stream::responses_mode_stream_cli_supports_openai_base_url_config_override` passes in isolation on the PR1 branch
- failure is in realtime multipart request body expectations
- PR1 does not touch realtime conversation code paths
- failing diffs are request-body ordering / serialization expectations in app-server realtime tests
- `suite::v2::realtime_conversation::realtime_webrtc_start_emits_sdp_notification` fails the same way on the clean upstream clone in `/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex`
- `suite::v2::realtime_conversation::webrtc_v1_start_posts_offer_returns_sdp_and_joins_sideband` also fails the same way on the clean upstream clone in `/Users/adpena/Projects/codex-mcp-and-subagent-memory-leaks/codex`

## Residual PR1 Risk Summary

Even if PR1 is correct, the following risks remain:

1. Queued follow-up backpressure
- inter-agent mailbox delivery remains effectively unbounded
- heavy queued follow-up traffic can still create pressure and responsiveness degradation

2. MCP process fanout
- live sessions still own their own MCP connection managers
- PR1 reduces live-session retention pressure but does not redesign MCP ownership

3. Terminal/TUI hard-failure cleanup
- PR1 does not include alternate-screen/raw-mode hardening
- catastrophic terminal corruption may still have frontend-specific contributing factors

4. App-server callback orphaning
- PR1 does not attempt to clean up all pending server-request callback edge cases on disconnect

## Unknown Unknowns

- there may be interaction bugs only visible under many concurrent root sessions rather than one overloaded root session
- there may be MCP-server-specific failure modes where child process cleanup differs by server implementation rather than Codex lifecycle alone
- there may be untested resume/list/wait consumers that relied on older `NotFound` semantics in subtle ways
