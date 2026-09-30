"""Provider-neutral supervision for private inference workers."""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import signal
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol

import httpx

from .protocol import LaunchSpec


class WorkerError(RuntimeError):
    """A worker could not be launched, reached, or stopped cleanly."""


@dataclass(frozen=True, slots=True)
class WorkerEndpoint:
    host: str
    port: int

    def __post_init__(self) -> None:
        try:
            loopback = self.host.lower() == "localhost" or ipaddress.ip_address(
                self.host
            ).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("engine worker endpoints must be loopback-only")
        if not 1 <= self.port <= 65535:
            raise ValueError("engine worker port must be between 1 and 65535")

    @property
    def base_url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"http://{host}:{self.port}"


@dataclass(slots=True)
class WorkerState:
    provider: str
    deployment: str | None = None
    phase: str = "stopped"
    pid: int | None = None
    error: str | None = None
    changed_at: float = 0.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    ready: bool
    detail: str | None = None
    version: str | None = None


class WorkerProbe(Protocol):
    path: str

    def inspect(self, status_code: int, payload: Any) -> ProbeResult: ...


class ManagedWorker:
    """Own exactly one loopback worker process group.

    This class knows process and network invariants, but no provider flags or
    model semantics. An engine adapter creates the :class:`LaunchSpec`; a
    provider probe defines readiness.
    """

    def __init__(
        self,
        *,
        provider: str,
        endpoint: WorkerEndpoint,
        probe: WorkerProbe,
        log_path: Path,
        state_path: Path,
        load_timeout_seconds: float = 240.0,
        stop_timeout_seconds: float = 25.0,
    ) -> None:
        self.provider = provider
        self.endpoint = endpoint
        self.probe = probe
        self.log_path = log_path
        self.state_path = state_path
        self.load_timeout_seconds = load_timeout_seconds
        self.stop_timeout_seconds = stop_timeout_seconds
        self.client = httpx.AsyncClient(
            base_url=endpoint.base_url,
            timeout=httpx.Timeout(30.0, read=None),
        )
        self.lifecycle_lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.log_handle: BinaryIO | None = None
        self.log_start_offset = 0
        self.state = self._read_state()
        # A PID persisted by an earlier gateway is evidence, not ownership.
        self.state.pid = None
        if self.state.phase not in {"stopped", "unloaded"}:
            self.state.phase = "external"

    def _read_state(self) -> WorkerState:
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            if raw.get("provider") != self.provider:
                return WorkerState(provider=self.provider)
            known = WorkerState.__dataclass_fields__
            return WorkerState(**{key: value for key, value in raw.items() if key in known})
        except (OSError, ValueError, TypeError):
            return WorkerState(provider=self.provider)

    def _write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        temporary.write_text(json.dumps(asdict(self.state), indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, self.state_path)

    def _owned_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def _port_is_open(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self.endpoint.host, self.endpoint.port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, TimeoutError):
            return False

    async def status(self) -> dict[str, Any]:
        owned = self._owned_running()
        try:
            response = await self.client.get(self.probe.path, timeout=3.0)
            try:
                payload: Any = response.json()
            except ValueError:
                payload = response.text
            result = self.probe.inspect(response.status_code, payload)
            return {
                "provider": self.provider,
                "reachable": True,
                "ready": result.ready,
                "owned": owned,
                "pid": self.process.pid if owned and self.process else None,
                "version": result.version,
                "detail": result.detail,
            }
        except httpx.HTTPError as exc:
            return {
                "provider": self.provider,
                "reachable": False,
                "ready": False,
                "owned": owned,
                "pid": self.process.pid if owned and self.process else None,
                "error": str(exc),
            }

    def _log_tail(self, limit: int = 4000) -> str:
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - limit))
                return handle.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    def launch_log(self) -> str:
        """Return only telemetry emitted by the current owned launch."""

        try:
            with self.log_path.open("rb") as handle:
                handle.seek(self.log_start_offset)
                return handle.read().decode("utf-8", errors="replace")
        except OSError:
            return ""

    async def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.load_timeout_seconds
        last_error = f"{self.provider} did not become ready"
        while time.monotonic() < deadline:
            if self.process and self.process.returncode is not None:
                tail = self._log_tail()
                raise WorkerError(
                    f"{self.provider} exited with status {self.process.returncode}"
                    + (f":\n{tail}" if tail else "")
                )
            try:
                response = await self.client.get(self.probe.path, timeout=3.0)
                try:
                    payload: Any = response.json()
                except ValueError:
                    payload = response.text
                result = self.probe.inspect(response.status_code, payload)
                if result.ready:
                    return
                if result.detail:
                    last_error = result.detail
            except httpx.HTTPError as exc:
                last_error = str(exc)
            await asyncio.sleep(0.5)
        raise WorkerError(last_error)

    async def _stop_owned_process(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            self.process = None
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), timeout=self.stop_timeout_seconds)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    async def start(self, deployment: str, launch: LaunchSpec) -> None:
        async with self.lifecycle_lock:
            self.state.deployment = deployment
            self.state.phase = "starting"
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()
            try:
                if not launch.executable.is_file():
                    raise WorkerError(f"worker executable not found: {launch.executable}")
                if not os.access(launch.executable, os.X_OK):
                    raise WorkerError(
                        f"worker executable is not executable: {launch.executable}"
                    )
                if await self._port_is_open() and not self._owned_running():
                    raise WorkerError(
                        f"{self.endpoint.host}:{self.endpoint.port} is occupied by a process "
                        "ENGRAI does not own"
                    )

                await self._stop_owned_process()
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.log_start_offset = self.log_path.stat().st_size
                except OSError:
                    self.log_start_offset = 0
                self.log_handle = self.log_path.open("ab", buffering=0)
                environment: Mapping[str, str] = {**os.environ, **launch.environment}
                self.process = await asyncio.create_subprocess_exec(
                    *launch.command(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=self.log_handle,
                    stderr=asyncio.subprocess.STDOUT,
                    env=environment,
                    start_new_session=True,
                )
                self.state.pid = self.process.pid
                self._write_state()
                await self._wait_until_ready()
            except Exception as exc:
                await self._stop_owned_process()
                self.state.phase = "error"
                self.state.pid = None
                self.state.error = str(exc)
                self.state.changed_at = time.time()
                self._write_state()
                raise
            self.state.phase = "ready"
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()

    async def stop(self) -> None:
        async with self.lifecycle_lock:
            await self._stop_owned_process()
            self.state.deployment = None
            self.state.phase = "unloaded"
            self.state.pid = None
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()

    async def close(self) -> None:
        await self.stop()
        await self.client.aclose()
