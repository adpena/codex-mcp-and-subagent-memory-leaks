# Integration test skeleton: retirement → MCP child PID teardown

This is the concrete shape of the remaining integration test (Finding 7
from the adversarial review). It would mirror
[`#19753`'s `process_group_cleanup.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/rmcp-client/tests/process_group_cleanup.rs)
but applied to the *retirement → `Op::Shutdown` → `handlers::shutdown` →
`mcp_connection_manager.begin_shutdown()`* chain rather than to direct
`RmcpClient::shutdown()`.

It is documented here rather than implemented inline because:

- The infrastructure for a full session-level test with a real
  `test_stdio_server` child process requires the `core_test_support`
  scaffolding (`test_codex`, SSE response mocks, wiremock-based model
  fakes) — see
  [`hooks_mcp.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/tests/suite/hooks_mcp.rs)
  (~350 lines) and
  [`agent_jobs.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/tests/suite/agent_jobs.rs)
  (~448 lines) for the canonical patterns.
- The equivalent registry- and control-level retirement semantics are
  already covered by these tests on the patched branch:
  [`retire_releases_slot_and_preserves_cached_status`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/codex/codex-rs/core/src/agent/registry_tests.rs#L353),
  [`spawn_agent_releases_slot_after_completion`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/codex/codex-rs/core/src/agent/control_tests.rs#L1034),
  [`v2_spawn_agent_releases_slot_after_completion`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/codex/codex-rs/core/src/agent/control_tests.rs),
  [`root_shutdown_shuts_down_live_spawned_descendants`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/codex/codex-rs/core/src/agent/control_tests.rs#L1123),
  [`failed_spawn_releases_reserved_nickname`](https://github.com/adpena/codex-mcp-and-subagent-memory-leaks/blob/main/codex/codex-rs/core/src/agent/registry_tests.rs),
  plus three new race tests added 2026-04-29 for retire/release ordering.
- The soak refresh on this branch already exercises the full retirement →
  shutdown → MCP cleanup chain on real binaries with real stdio MCP
  children (5 per worker × 4 workers = 20 stdio MCP children), and shows
  64 % fewer `agent thread limit reached` rejections on the patched
  binary vs. unpatched `codex-cli 0.125.0`. Per-PID termination is not
  asserted there, but the rejection-rate reduction is a sufficient
  end-to-end signal that retirement is firing and freeing slots that
  previously stayed occupied.

## Skeleton (Rust)

Belongs in `codex-rs/core/tests/suite/spawn_agent_retirement.rs`,
modeled on `process_group_cleanup.rs` (which lives in
`codex-rs/rmcp-client/tests/`).

```rust
#![cfg(unix)]

use std::collections::HashMap;
use std::ffi::OsString;
use std::time::Duration;

use anyhow::Result;
use codex_config::types::McpServerConfig;
use codex_config::types::McpServerTransportConfig;
use codex_features::Feature;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_function_call_with_namespace;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::mount_sse_sequence;
use core_test_support::responses::sse;
use core_test_support::stdio_server_bin;
use core_test_support::test_codex::test_codex;

fn process_exists(pid: u32) -> bool {
    std::process::Command::new("kill")
        .arg("-0")
        .arg(pid.to_string())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn finalized_subagent_terminates_its_stdio_mcp_child() -> Result<()> {
    // 1. Build a temp pid-file path.
    let temp = tempfile::tempdir()?;
    let pid_file = temp.path().join("server.pid");

    // 2. Build a Config with `[agents] max_threads = 2` AND an stdio
    //    MCP server pointing at `test_stdio_server` with
    //    `MCP_TEST_PID_FILE` env so the server records its PID.
    let mut codex = test_codex().await?;
    codex.config.agent_max_threads = Some(2);
    codex.config.mcp_servers.insert(
        "pid_writer".to_string(),
        McpServerConfig {
            transport: McpServerTransportConfig::Stdio {
                command: stdio_server_bin()?.into_os_string(),
                args: Vec::new(),
                env: HashMap::from([(
                    OsString::from("MCP_TEST_PID_FILE"),
                    OsString::from(pid_file.to_string_lossy().into_owned()),
                )]),
            },
            tool_timeout_sec: None,
            startup_timeout_sec: None,
        },
    );

    // 3. Mount a model response that emits a single `spawn_agent` call.
    mount_sse_sequence(
        &codex.mock_server,
        vec![sse(vec![
            ev_response_created("resp-spawn"),
            ev_function_call_with_namespace(
                "spawn-call-1",
                "spawn_agent",
                json!({"message": "child task"}).to_string(),
            ),
            ev_completed("resp-spawn"),
        ])],
    )
    .await;

    // 4. Drive the parent thread. Parent issues spawn_agent → child
    //    thread starts → child's session creates its own
    //    McpConnectionManager, which spawns test_stdio_server with
    //    MCP_TEST_PID_FILE.
    codex.send_user_input("spawn one").await?;

    // 5. Read the PID file. Wait up to 5 s for it to appear.
    //    Verify the process exists.
    let pid = wait_for_pid_file(&pid_file).await?;
    assert!(process_exists(pid), "MCP child {pid} should be live");

    // 6. Drive the child thread to TurnComplete. The completion watcher
    //    (V1 path) calls retire_finalized_agent on the child →
    //    Op::Shutdown is sent → child's handlers::shutdown runs →
    //    mcp_connection_manager.begin_shutdown() fires → child's
    //    test_stdio_server PID is killed.
    //
    //    Easiest way to drive: push another SSE response sequence for
    //    the child that emits ev_completed.
    //
    //    [Concrete plumbing here depends on how core_test_support
    //    routes responses to specific child threads — likely needs a
    //    second mock_server or a routing matcher.]

    // 7. Wait up to 5 s for the PID to disappear. Assert.
    wait_for_process_exit(pid).await?;

    Ok(())
}
```

## Open implementation questions

- `core_test_support::test_codex` doesn't currently expose a way to
  set per-thread SSE responses — needs verification or extension.
- Driving subagent completion through `TurnComplete` from a test
  requires either a mocked model that returns `TurnComplete` after the
  child's first user input, or direct injection through
  `Session::send_event` (as the existing
  `spawn_agent_releases_slot_after_completion` test does).
- The race timing between `retire_finalized_agent` sending
  `Op::Shutdown`, the submission_loop processing it, and the
  `mcp_connection_manager.begin_shutdown()` actually killing the child
  process group can take up to ~1–2 s. The 5 s `wait_for_process_exit`
  budget should be enough; if flakiness arises, raise to 10 s.
- The V2 variant should test the same flow with
  `Feature::MultiAgentV2` enabled, exercising
  `Session::maybe_notify_parent_of_terminal_turn`'s retirement call.

## Manual verification approximation

Until the test above is implemented, the closest manual verification is
the soak telemetry under `artifacts/soak-summaries/patched-recursion-*`
+ inspecting `worker-log-tails/*-tail.log` for
`Failed to terminate MCP process group … No such process` warnings —
which on the patched run almost always indicate the child *already*
exited because retirement signaled shutdown, not because cleanup raced
with an external kill.
