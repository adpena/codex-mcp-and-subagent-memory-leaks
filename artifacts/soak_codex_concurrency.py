#!/usr/bin/env python3
"""Hostile Codex soak driver for subagent/MCP accumulation issues.

This script is intentionally pragmatic rather than polished. It exists to:
- launch many Codex sessions concurrently;
- force recursive subagent spawning and/or hanging stdio MCP calls;
- sample process state over time;
- report what is still alive after shutdown.

The script uses CODEX_RS_SSE_FIXTURE so it can stress behavior without relying
on live model traffic. It writes all temporary state under a single temp root
and preserves that root by default for post-mortem inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import dataclass
from typing import Iterable


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUST_ROOT = ROOT / "codex-rs"
DEFAULT_CODEX_BIN = RUST_ROOT / "target" / "debug" / "codex"
DEFAULT_STDIO_SERVER_BIN = RUST_ROOT / "target" / "debug" / "test_stdio_server"
PROJECT_ROOT = ROOT


@dataclass
class Worker:
    name: str
    scenario: str
    codex_home: pathlib.Path
    fixture_path: pathlib.Path
    log_path: pathlib.Path
    process: subprocess.Popen[bytes]


LIFECYCLE_PATTERNS = {
    "agent_slot_reserved": re.compile(r"reserved spawned-agent slot"),
    "agent_slot_released": re.compile(r"released spawned-agent slot"),
    "mcp_spawned": re.compile(r"spawned stdio MCP server process"),
    "mcp_dropped": re.compile(r"dropping MCP process-group guard"),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workers", type=int, default=6, help="number of concurrent sessions")
    parser.add_argument(
        "--duration-sec", type=int, default=120, help="soak duration before teardown"
    )
    parser.add_argument(
        "--spawn-interval-ms",
        type=int,
        default=400,
        help="delay between launching workers",
    )
    parser.add_argument(
        "--sample-interval-sec",
        type=float,
        default=2.0,
        help="process sampling interval",
    )
    parser.add_argument(
        "--scenarios",
        default="spawn_then_hang,spawn_recursive,hang_only",
        help="comma-separated scenario cycle",
    )
    parser.add_argument(
        "--codex-bin",
        type=pathlib.Path,
        default=DEFAULT_CODEX_BIN,
        help="path to built codex binary",
    )
    parser.add_argument(
        "--stdio-server-bin",
        type=pathlib.Path,
        default=DEFAULT_STDIO_SERVER_BIN,
        help="path to built test_stdio_server binary",
    )
    parser.add_argument(
        "--out-dir",
        type=pathlib.Path,
        default=None,
        help="optional output directory; defaults to a temp dir",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="delete the output directory after a clean run",
    )
    parser.add_argument(
        "--debug-lifecycle",
        action="store_true",
        help="enable verbose lifecycle logging inside Codex",
    )
    return parser.parse_args()


def ensure_binary(path: pathlib.Path, package: str, binary: str) -> pathlib.Path:
    if path.is_file():
        return path
    cmd = ["cargo", "build", "-p", package, "--bin", binary]
    print(f"[build] {' '.join(cmd)}", file=sys.stderr)
    subprocess.run(cmd, cwd=RUST_ROOT, check=True)
    if not path.is_file():
        raise FileNotFoundError(f"expected binary at {path}")
    return path


def make_config(codex_home: pathlib.Path, stdio_server_bin: pathlib.Path) -> None:
    config = textwrap.dedent(
        f"""
        model_provider = "openai"
        approval_policy = "never"
        sandbox_mode = "danger-full-access"

        [projects."{PROJECT_ROOT}"]
        trust_level = "trusted"

        [mcp_servers.hang_server]
        command = "{stdio_server_bin}"
        tool_timeout_sec = 300
        """
    ).strip()
    (codex_home / "config.toml").write_text(config + "\n", encoding="utf-8")


def fixture_events_for_scenario(scenario: str) -> list[dict]:
    events: list[dict] = [
        {"type": "response.created", "response": {"id": f"resp-{scenario}"}},
    ]

    if scenario in {"spawn_recursive", "spawn_then_hang"}:
        events.append(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": f"spawn-{scenario}",
                    "name": "spawn_agent",
                    "arguments": json.dumps({"message": f"{scenario}: recurse"}),
                },
            }
        )

    if scenario in {"hang_only", "spawn_then_hang"}:
        events.append(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": f"hang-{scenario}",
                    "name": "mcp__hang_server__hang",
                    "arguments": "{}",
                },
            }
        )

    events.append({"type": "response.completed", "response": {"id": f"resp-{scenario}", "output": []}})
    return events


def write_fixture(path: pathlib.Path, scenario: str) -> None:
    chunks: list[str] = []
    for event in fixture_events_for_scenario(scenario):
        event_name = event["type"]
        chunks.append(f"event: {event_name}\ndata: {json.dumps(event)}\n")
    path.write_text("\n".join(chunks), encoding="utf-8")


def scenario_cycle(raw: str) -> list[str]:
    values = [part.strip() for part in raw.split(",") if part.strip()]
    if not values:
        raise ValueError("at least one scenario is required")
    return values


def start_worker(
    index: int,
    scenario: str,
    out_dir: pathlib.Path,
    codex_bin: pathlib.Path,
    stdio_server_bin: pathlib.Path,
    debug_lifecycle: bool,
) -> Worker:
    name = f"worker-{index:02d}"
    codex_home = out_dir / name / "codex_home"
    codex_home.mkdir(parents=True, exist_ok=True)
    make_config(codex_home, stdio_server_bin)

    fixture_path = out_dir / name / f"{scenario}.sse"
    write_fixture(fixture_path, scenario)

    log_path = out_dir / name / "codex.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = log_path.open("wb")

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    env["OPENAI_API_KEY"] = "dummy"
    env["CODEX_RS_SSE_FIXTURE"] = str(fixture_path)
    if debug_lifecycle:
        env["CODEX_DEBUG_AGENT_LIFECYCLE"] = "1"
        env["CODEX_DEBUG_MCP_LIFECYCLE"] = "1"
        env["CODEX_DEBUG_THREAD_LISTENERS"] = "1"
        env["RUST_LOG"] = ",".join(
            [
                "error",
                "codex_core::agent::registry=info",
                "codex_rmcp_client=info",
                "codex_tui::app=info",
            ]
        )

    cmd = [
        str(codex_bin),
        "exec",
        "--skip-git-repo-check",
        "-C",
        str(PROJECT_ROOT),
        f"{scenario} soak worker {index}",
        "-c",
        "analytics.enabled=false",
    ]
    process = subprocess.Popen(
        cmd,
        cwd=PROJECT_ROOT,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    return Worker(
        name=name,
        scenario=scenario,
        codex_home=codex_home,
        fixture_path=fixture_path,
        log_path=log_path,
        process=process,
    )


def sample_process_table() -> list[dict]:
    result = subprocess.run(
        ["ps", "-axo", "pid,ppid,pgid,rss,vsz,stat,etime,command"],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = result.stdout.splitlines()
    records: list[dict] = []
    for line in lines[1:]:
        parts = line.strip().split(None, 7)
        if len(parts) != 8:
            continue
        pid, ppid, pgid, rss, vsz, stat, etime, command = parts
        if "target/debug/codex" not in command and "test_stdio_server" not in command:
            continue
        records.append(
            {
                "pid": int(pid),
                "ppid": int(ppid),
                "pgid": int(pgid),
                "rss_kb": int(rss),
                "vsz_kb": int(vsz),
                "stat": stat,
                "etime": etime,
                "command": command,
            }
        )
    return records


def write_samples(path: pathlib.Path, samples: Iterable[dict]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample) + "\n")


def terminate_worker(worker: Worker, sig: int) -> None:
    if worker.process.poll() is not None:
        return
    try:
        os.killpg(worker.process.pid, sig)
    except ProcessLookupError:
        return


def terminate_workers(workers: list[Worker], grace_sec: float) -> None:
    for worker in workers:
        terminate_worker(worker, signal.SIGINT)
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if all(worker.process.poll() is not None for worker in workers):
            return
        time.sleep(0.2)
    for worker in workers:
        terminate_worker(worker, signal.SIGTERM)
    deadline = time.monotonic() + grace_sec
    while time.monotonic() < deadline:
        if all(worker.process.poll() is not None for worker in workers):
            return
        time.sleep(0.2)
    for worker in workers:
        terminate_worker(worker, signal.SIGKILL)


def collect_summary(out_dir: pathlib.Path, workers: list[Worker], samples_path: pathlib.Path) -> dict:
    process_records = sample_process_table()
    summary = {
        "out_dir": str(out_dir),
        "workers": [
            {
                "name": worker.name,
                "scenario": worker.scenario,
                "pid": worker.process.pid,
                "exit_code": worker.process.poll(),
                "log_path": str(worker.log_path),
                "lifecycle_counts": count_lifecycle_events(worker.log_path),
            }
            for worker in workers
        ],
        "survivors": process_records,
        "samples_path": str(samples_path),
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def count_lifecycle_events(log_path: pathlib.Path) -> dict[str, int]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return {
        name: len(pattern.findall(text))
        for name, pattern in LIFECYCLE_PATTERNS.items()
    }


def main() -> int:
    args = parse_args()
    codex_bin = ensure_binary(args.codex_bin, "codex-cli", "codex")
    stdio_server_bin = ensure_binary(args.stdio_server_bin, "codex-rmcp-client", "test_stdio_server")

    tmp_root = ROOT / ".tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    out_dir = args.out_dir or pathlib.Path(
        tempfile.mkdtemp(prefix="codex-soak-", dir=str(tmp_root))
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    samples_path = out_dir / "process_samples.jsonl"

    print(f"[soak] output: {out_dir}")
    scenarios = scenario_cycle(args.scenarios)
    workers: list[Worker] = []
    start = time.monotonic()
    next_sample = start

    try:
        for index in range(args.workers):
            scenario = scenarios[index % len(scenarios)]
            worker = start_worker(
                index,
                scenario,
                out_dir,
                codex_bin,
                stdio_server_bin,
                args.debug_lifecycle,
            )
            workers.append(worker)
            print(
                f"[launch] {worker.name} scenario={worker.scenario} pid={worker.process.pid}",
                flush=True,
            )
            time.sleep(args.spawn_interval_ms / 1000.0)

        end = start + args.duration_sec
        while time.monotonic() < end:
            now = time.monotonic()
            if now >= next_sample:
                samples = sample_process_table()
                write_samples(
                    samples_path,
                    [
                        {
                            "ts": time.time(),
                            "records": samples,
                        }
                    ],
                )
                print(
                    f"[sample] alive_workers={sum(worker.process.poll() is None for worker in workers)} "
                    f"tracked_processes={len(samples)}",
                    flush=True,
                )
                next_sample = now + args.sample_interval_sec
            time.sleep(0.2)
    finally:
        terminate_workers(workers, grace_sec=5.0)
        summary = collect_summary(out_dir, workers, samples_path)
        print(
            f"[summary] survivors={len(summary['survivors'])} summary={out_dir / 'summary.json'}",
            flush=True,
        )
        if args.cleanup and not summary["survivors"]:
            shutil.rmtree(out_dir, ignore_errors=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
