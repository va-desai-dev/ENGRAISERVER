from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from engrai_server.engines.llamacpp import LlamaCppProbe
from engrai_server.engines.protocol import LaunchSpec
from engrai_server.engines.worker import ManagedWorker, WorkerEndpoint, WorkerError


@pytest.mark.asyncio
async def test_managed_worker_owns_llama_server_lifecycle(tmp_path: Path) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    executable = tmp_path / "llama-server-fake"
    executable.write_text(
        """#!/usr/bin/python3
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

args = sys.argv[1:]
host = args[args.index('--host') + 1]
port = int(args[args.index('--port') + 1])

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass
    def do_GET(self):
        if self.path == '/health':
            body = json.dumps({'status': 'ok'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

HTTPServer((host, port), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    endpoint = WorkerEndpoint("127.0.0.1", port)
    state_path = tmp_path / "state" / "worker.json"
    worker = ManagedWorker(
        provider="llama.cpp",
        endpoint=endpoint,
        probe=LlamaCppProbe(),
        log_path=tmp_path / "state" / "worker.log",
        state_path=state_path,
        load_timeout_seconds=5,
        stop_timeout_seconds=2,
    )
    launch = LaunchSpec(
        executable,
        ("--host", endpoint.host, "--port", str(endpoint.port)),
        {},
    )

    await worker.start("example-local", launch)
    status = await worker.status()
    assert status["provider"] == "llama.cpp"
    assert status["ready"] is True
    assert status["owned"] is True
    assert worker.state.phase == "ready"
    assert worker.launch_log() == ""
    assert json.loads(state_path.read_text())["deployment"] == "example-local"
    assert state_path.stat().st_mode & 0o777 == 0o600

    await worker.close()
    assert worker.state.phase == "unloaded"


def test_llamacpp_probe_distinguishes_loading_from_ready() -> None:
    probe = LlamaCppProbe()
    loading = probe.inspect(
        503,
        {"error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}},
    )
    assert loading.ready is False
    assert loading.detail == "Loading model"
    assert probe.inspect(200, {"status": "ok"}).ready is True


def test_worker_endpoint_rejects_public_bind() -> None:
    with pytest.raises(ValueError, match="loopback-only"):
        WorkerEndpoint("0.0.0.0", 5002)


@pytest.mark.asyncio
async def test_worker_persists_pre_spawn_failures(tmp_path: Path) -> None:
    worker = ManagedWorker(
        provider="llama.cpp",
        endpoint=WorkerEndpoint("127.0.0.1", 51999),
        probe=LlamaCppProbe(),
        log_path=tmp_path / "worker.log",
        state_path=tmp_path / "worker.json",
        load_timeout_seconds=1,
        stop_timeout_seconds=1,
    )
    launch = LaunchSpec(tmp_path / "missing-llama-server", (), {})

    with pytest.raises(WorkerError, match="executable not found"):
        await worker.start("broken", launch)
    assert worker.state.phase == "error"
    assert worker.state.deployment == "broken"
    assert "executable not found" in (worker.state.error or "")
    await worker.client.aclose()


def test_launch_log_excludes_telemetry_from_previous_runs(tmp_path: Path) -> None:
    log_path = tmp_path / "worker.log"
    log_path.write_text("old launch\n", encoding="utf-8")
    worker = ManagedWorker(
        provider="llama.cpp",
        endpoint=WorkerEndpoint("127.0.0.1", 51998),
        probe=LlamaCppProbe(),
        log_path=log_path,
        state_path=tmp_path / "worker.json",
    )
    worker.log_start_offset = log_path.stat().st_size
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write("current launch\n")

    assert worker.launch_log() == "current launch\n"
