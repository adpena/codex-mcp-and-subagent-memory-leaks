## What version of Codex CLI is running?

Built from source at `openai/codex@80fb0704ee` (current `main` as of 2026-04-29).

## What subscription do you have?

ChatGPT Pro

## Which model were you using?

N/A — test-binary failure, no model interaction.

## What platform is your computer?

`Darwin 25.4.0 arm64 arm` (macOS 26.4, Apple Silicon, 128 GB RAM)

## What terminal emulator and version are you using?

Ghostty 1.3.1

## What issue are you seeing?

`cargo test -p codex-tui --lib` (and `cargo test --workspace --no-fail-fast`) aborts the test binary mid-run with `signal: 6, SIGABRT: process abort signal` after running ~2009-2018 of the 2018 unit tests. No panic stack is printed; the last test name in stdout varies per run.

```
running 2018 tests
test additional_dirs::tests::returns_none_for_danger_full_access ... ok
test additional_dirs::tests::returns_none_for_external_sandbox ... ok
[... ~2000 tests later, varies per run ...]

error: test failed, to rerun pass `-p codex-tui --lib`

Caused by:
  process didn't exit successfully:
    `target/debug/deps/codex_tui-<hash>` (signal: 6, SIGABRT: process abort signal)
```

Reproducible on a fresh clone, both default parallelism and `--test-threads=1`.

## What steps can reproduce the bug?

```bash
git clone --depth 1 https://github.com/openai/codex.git
cd codex
git fetch --depth 1 origin 80fb0704ee8b23ab7cbc3f2c4dcdbf3c1a5fbd4b
git checkout 80fb0704ee
cd codex-rs
cargo test -p codex-tui --lib 2>&1 | tail -10
```

## What is the expected behavior?

`cargo test -p codex-tui --lib` should complete with a `test result: ok` / `FAILED` summary listing per-test results, not abort the binary.

## Additional information

Ruled out as causes:

- **Resource exhaustion.** macOS file descriptor limit is `1048576` (`ulimit -n`); process limit `10666` (`ulimit -u`). System has 128 GB RAM and zero swap pressure during the run (`vm.swapusage: total = 0.00M used = 0.00M`).
- **Specific failing test.** The last test printed before the abort varies per run (e.g., `external_editor::tests::run_editor_returns_updated_content` one run, `bottom_pane::textarea::tests::fuzz_textarea_randomized` another). Non-deterministic; not a single-test panic.

Possible causes worth investigating:

- A test calling `std::process::abort()` or `panic!` inside a `Drop` implementation invoked during runtime teardown.
- A static destructor (e.g., a `OnceCell<Mutex<...>>` poisoned by a panicked test) aborting during `Drop` of test runtime.
- A `tokio::Runtime::shutdown_*` interaction aborting under specific test interleavings.
- An `assert!` in unsafe code path (transmute, raw FFI) short-circuiting via abort rather than panic.

`RUST_BACKTRACE=full` and core-dump capture available on request.

Build: `cargo +1.93.0-aarch64-apple-darwin test`, default debug profile.

— Alejandro Pena ([@adpena](https://github.com/adpena))
