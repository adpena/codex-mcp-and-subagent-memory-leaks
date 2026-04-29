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
modeled on `process_group_cleanup.rs` (`codex-rs/rmcp-client/tests/`).

The pseudocode below uses real APIs from this repo:
[`TestCodexBuilder`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/tests/common/test_codex.rs)
(`test_codex()` returns a builder, `build()` is async),
[`McpServerConfig`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/config/src/mcp_types.rs)
(`McpServerTransportConfig::Stdio` takes `String`, not `OsString`;
`env: Option<HashMap<String, String>>`), `Constrained::set` for
`config.mcp_servers`, and the four-argument
`ev_function_call_with_namespace(call_id, namespace, name, arguments)`.

```rust
#![cfg(unix)]

use std::collections::HashMap;
use std::time::Duration;

use anyhow::Result;
use codex_config::types::McpServerConfig;
use codex_config::types::McpServerTransportConfig;
use core_test_support::responses::ev_completed;
use core_test_support::responses::ev_function_call_with_namespace;
use core_test_support::responses::ev_response_created;
use core_test_support::responses::mount_sse_sequence;
use core_test_support::responses::sse;
use core_test_support::responses::start_mock_server;
use core_test_support::stdio_server_bin;
use core_test_support::test_codex::test_codex;
use serde_json::json;
use tokio::time::sleep;

fn process_exists(pid: u32) -> bool {
    std::process::Command::new("kill")
        .arg("-0")
        .arg(pid.to_string())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

async fn wait_for_pid_file(path: &std::path::Path) -> Result<u32> {
    for _ in 0..50 {
        if let Ok(text) = std::fs::read_to_string(path) {
            let trimmed = text.trim();
            if !trimmed.is_empty() {
                return Ok(trimmed.parse::<u32>()?);
            }
        }
        sleep(Duration::from_millis(100)).await;
    }
    anyhow::bail!("timed out waiting for pid file at {}", path.display())
}

async fn wait_for_process_exit(pid: u32) -> Result<()> {
    for _ in 0..100 {
        if !process_exists(pid) {
            return Ok(());
        }
        sleep(Duration::from_millis(100)).await;
    }
    anyhow::bail!("process {pid} still alive after timeout")
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn finalized_subagent_terminates_its_stdio_mcp_child() -> Result<()> {
    // 1. Bring up the mock model server.
    let server = start_mock_server().await;

    // 2. Temp dir for the child MCP server's pid file.
    let temp = tempfile::tempdir()?;
    let pid_file = temp.path().join("server.pid");
    let pid_file_str = pid_file.to_string_lossy().into_owned();
    let stdio_bin = stdio_server_bin()?;
    let stdio_bin_str = stdio_bin.to_string_lossy().into_owned();

    // 3. Build the test session via the real builder. Use `with_config`
    //    (or the equivalent mutator on TestCodexBuilder) to attach an
    //    stdio MCP server pointing at test_stdio_server with
    //    MCP_TEST_PID_FILE so the server records its PID.
    let codex = test_codex()
        // Hook: install MCP servers BEFORE build() runs the config-load
        // sequence. `mcp_servers` is `Constrained<HashMap<...>>`; use
        // `set()`, not `insert`/`DerefMut`.
        .with_config_mutator(move |cfg| {
            let mut servers = cfg.mcp_servers.get().clone();
            servers.insert(
                "pid_writer".to_string(),
                McpServerConfig {
                    transport: McpServerTransportConfig::Stdio {
                        command: stdio_bin_str.clone(),
                        args: Vec::new(),
                        env: Some(HashMap::from([(
                            "MCP_TEST_PID_FILE".to_string(),
                            pid_file_str.clone(),
                        )])),
                    },
                    tool_timeout_sec: None,
                    startup_timeout_sec: None,
                },
            );
            cfg.mcp_servers.set(servers);
            // Enable V1 multi-agents with max_threads=1 so spawn_agent
            // is registered and the slot-retention path is exercised.
            cfg.agent_max_threads = Some(1);
        })
        .with_mock_server(&server)
        .build()
        .await?;

    // 4. Mount the parent SSE response: emit one spawn_agent call,
    //    then complete the parent's turn. Mount a separate SSE for
    //    the child thread that emits a TurnComplete immediately.
    //    (core_test_support routes by request matcher; see
    //    spawn_agent_description.rs for an equivalent two-thread
    //    pattern.)
    mount_sse_sequence(
        &server,
        /*req_matcher*/ /* parent thread */ /* ... */,
        vec![sse(vec![
            ev_response_created("resp-parent"),
            ev_function_call_with_namespace(
                "spawn-call-1",
                /*namespace*/ "",
                /*name*/ "spawn_agent",
                /*arguments*/ &json!({"message": "child task"}).to_string(),
            ),
            ev_completed("resp-parent"),
        ])],
    )
    .await;
    mount_sse_sequence(
        &server,
        /*req_matcher*/ /* child thread */ /* ... */,
        vec![sse(vec![
            ev_response_created("resp-child"),
            ev_completed("resp-child"),
        ])],
    )
    .await;

    // 5. Drive the parent. Parent issues spawn_agent -> child thread
    //    starts -> child's SessionServices builds its own
    //    McpConnectionManager which spawns test_stdio_server with
    //    MCP_TEST_PID_FILE.
    codex.send_user_input("spawn one").await?;

    // 6. Read child's MCP PID. Verify it's live.
    let pid = wait_for_pid_file(&pid_file).await?;
    assert!(process_exists(pid), "MCP child {pid} should be live");

    // 7. The child completes (its mounted SSE returns Completed).
    //    The V1 completion watcher calls retire_finalized_agent ->
    //    Op::Shutdown is enqueued on the child's submission channel ->
    //    child's handlers::shutdown runs -> #19753's
    //    mcp_connection_manager.begin_shutdown() terminates the
    //    process group.

    // 8. Wait for PID to disappear.
    wait_for_process_exit(pid).await?;

    Ok(())
}
```

## Open implementation questions

- `TestCodexBuilder` does not expose a documented "per-thread SSE
  matcher" API; the right routing primitive may need a small extension
  (or use `wiremock::matchers::body_partial_json` to match on the child
  vs. parent thread id in the request body). See
  [`agent_jobs.rs`](https://github.com/openai/codex/blob/80fb0704ee/codex-rs/core/tests/suite/agent_jobs.rs)
  for a working two-stage `Mock` + custom `Respond` pattern that could
  be adapted.
- The V1 watcher runs `retire_finalized_agent` from a `tokio::spawn`,
  so step 8's timeout has to cover one round-trip through the child's
  submission queue; 5–10 s should be sufficient.
- The V2 variant: enable `Feature::MultiAgentV2` instead of setting
  `agent_max_threads`. V2 retirement runs from
  `Session::maybe_notify_parent_of_terminal_turn`, also via
  `tokio::spawn` (after the deadlock fix on 2026-04-29).
- For determinism, prefer `tokio::time::pause()` if introducing
  `wait_for_*` polling becomes flaky.

## Manual verification approximation

Until the test above is implemented, the closest manual verification is
the soak telemetry under `artifacts/soak-summaries/patched-recursion-*`
+ inspecting `worker-log-tails/*-tail.log` for
`Failed to terminate MCP process group … No such process` warnings —
which on the patched run almost always indicate the child *already*
exited because retirement signaled shutdown, not because cleanup raced
with an external kill.
