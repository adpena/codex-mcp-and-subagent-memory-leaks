## PR1: Narrow Fix

Goal:
- stop finalized spawned agents from continuing to consume live slot/session ownership
- make root-session shutdown drain live spawned descendants more reliably
- preserve stable `wait_agent`, `list_agents`, and `resume_agent` behavior after retirement

Code scope:
- `codex-rs/core/src/agent/registry.rs`
- `codex-rs/core/src/agent/control.rs`
- `codex-rs/core/src/codex.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/wait.rs`
- `codex-rs/core/src/tools/handlers/multi_agents/resume_agent.rs`
- narrow regression coverage in `core/src/agent/control_tests.rs` and `core/src/agent/registry_tests.rs`

Key fixes:
- retire `Completed` / `Errored` spawned agents from live slot ownership while caching final status
- release reserved nicknames when spawn setup fails before commit
- perform bounded descendant shutdown sweeps when a root session shuts down
- clear remaining spawned-agent registry state after root-session teardown
- keep the existing upstream root/subagent boundary while tightening cleanup semantics

What PR1 intentionally does not fix:
- unbounded mailbox/backpressure behavior under large queued follow-up bursts
- per-session MCP process fanout architecture
- app-server disconnect cleanup for pending server-request callbacks
- terminal restore / alt-screen hardening after catastrophic failure

Remaining risk to document in PR1:
- the main accumulation path is reduced, but very high queued-message pressure can still degrade responsiveness because inter-agent mailbox delivery remains unbounded
- MCP-heavy sessions still multiply stdio server processes per live session
- terminal corruption after a hard crash can still have TUI-specific contributing factors outside the core leak path

## PR2: Broader Follow-up

Candidate follow-up tracks:

1. Backpressure and queue hygiene
- bound or otherwise meter inter-agent mailbox growth
- review `wait_agent` wakeups that trigger on mailbox sequence changes even when no real work starts
- test queued follow-up storms explicitly

2. App-server disconnect/orphan cleanup
- resolve pending callback / request cleanup when connections disappear mid-flight
- audit notification wait paths that depend on writer acknowledgements

3. Terminal/TUI hardening
- tighten terminal restore semantics around raw mode and alternate screen
- investigate event-loop starvation under high app-event churn

4. MCP fanout architecture
- determine whether spawned sessions should isolate, reuse, or lazily create MCP connection managers
- measure whether eager per-session MCP startup remains too expensive even after PR1
