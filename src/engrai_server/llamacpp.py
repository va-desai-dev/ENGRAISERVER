"""ENGRAI-owned llama.cpp text control plane.

The public gateway speaks ENGRAI and OpenAI-compatible HTTP.  The private
worker is a pinned ``llama-server`` binary installed from an ENGRAI runtime
bundle; it is never discovered from PATH and never exposed beyond loopback.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Coroutine
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import jinja2

from .domain.deployments import DeploymentProfile
from .domain.models import ModelManifest
from .domain.store import DeploymentStore
from .engines.llamacpp import (
    LlamaCppAdapter,
    LlamaCppDevice,
    LlamaCppInventoryError,
    LlamaCppProbe,
    chat,
    device_map,
    probe_devices,
    validate_extra_arguments,
)
from .engines.protocol import LaunchSpec
from .engines.worker import ManagedWorker, WorkerEndpoint, WorkerError
from .profiles import RouteProfile, ProfileStore
from .runtime import RuntimeManagerError, inspect_runtime
from .settings import Settings


class LlamaCppError(RuntimeError):
    """A direct ENGRAI text deployment could not be planned or operated."""


@dataclass(slots=True)
class RuntimeState:
    active_model: str | None = None
    phase: str = "stopped"
    target_model: str | None = None
    pid: int | None = None
    error: str | None = None
    changed_at: float = 0.0


def effective_cache_capabilities(
    deployment: DeploymentProfile,
    launch_log: str,
) -> dict[str, dict[str, Any]]:
    """Separate requested cache controls from architecture-effective behavior."""

    lower_log = launch_log.lower()
    shift_requested = deployment.memory.context_shift
    reuse_requested = deployment.memory.cache_reuse_min_tokens > 0
    shift_unsupported = any(
        marker in lower_log
        for marker in (
            "kv cache shifting is not supported",
            "context shift is not supported",
            "disabling context shift",
        )
    )
    reuse_unsupported = any(
        marker in lower_log
        for marker in (
            "cache_reuse is not supported",
            "cache reuse is not supported",
            "disabling cache reuse",
        )
    )

    def capability(requested: bool, unsupported: bool) -> dict[str, Any]:
        if not requested:
            detail = "not requested"
        elif unsupported:
            detail = "rejected by the loaded model architecture"
        else:
            detail = "accepted by the worker"
        return {
            "requested": requested,
            "effective": requested and not unsupported,
            "detail": detail,
        }

    return {
        # --cache-prompt is owned by the adapter and remains distinct from
        # arbitrary KV mutation. Its actual hit count is request telemetry.
        "exact_prefix_cache": {
            "requested": True,
            "effective": True,
            "detail": "enabled; per-request cached token counts prove hits",
        },
        "context_shift": capability(shift_requested, shift_unsupported),
        "chunk_reuse": capability(reuse_requested, reuse_unsupported),
    }


def _manifest_for(profile: RouteProfile) -> ModelManifest:
    """Represent an ad-hoc local GGUF with the portable adapter contract."""

    prompt = profile.prompt
    return ModelManifest.model_validate(
        {
            "schema_version": 2,
            "id": profile.id,
            "name": profile.name or profile.model_path.stem,
            "task": "chat",
            "format": "gguf",
            # Local files do not claim to be catalog artifacts.  This source
            # coordinate is an explicit compatibility bridge until a route is
            # bound to a reviewed @models manifest.
            "source": {
                "provider": "huggingface",
                "repository": "local/compatibility-import",
                "revision": "0000000",
            },
            "artifacts": {"model": {"filename": profile.model_path.name}},
            "prompt": prompt.model_dump(mode="json") if prompt else None,
        }
    )


def plan_deployment(
    deployment: DeploymentProfile,
    *,
    backend: str,
    devices: tuple[LlamaCppDevice, ...],
) -> DeploymentProfile:
    """Resolve safe ``auto`` choices against this exact runtime and host.

    The result is suitable for persistence: engine launch never contains a
    hidden auto-fit step.  When several accelerators are available, ENGRAI
    selects all of them and weights the layer split by physical VRAM.  An
    operator's explicit device list or split is preserved exactly.
    """

    planned = deployment.model_copy(deep=True)
    planned.engine = "llama.cpp"
    if planned.compute.backend == "auto":
        planned.compute.backend = backend  # type: ignore[assignment]
    if planned.compute.backend != "cpu" and planned.compute.backend != backend:
        raise LlamaCppError(
            f"active ENGRAI runtime uses {backend}, not {planned.compute.backend}"
        )

    if planned.compute.backend == "cpu":
        planned.compute.devices = []
        planned.compute.tensor_split = []
        planned.compute.main_gpu = None
        if planned.compute.gpu_layers == "auto":
            planned.compute.gpu_layers = 0
        return planned

    available = tuple(item for item in devices if item.backend == backend)
    by_index = {item.index: item for item in available}
    if not by_index:
        raise LlamaCppError(f"the active {backend} runtime reported no accelerator devices")
    if not planned.compute.devices:
        planned.compute.devices = sorted(by_index)
    missing = [index for index in planned.compute.devices if index not in by_index]
    if missing:
        raise LlamaCppError(
            "selected devices are unavailable: " + ", ".join(map(str, missing))
        )
    if planned.compute.gpu_layers == "auto":
        planned.compute.gpu_layers = "all"
    if len(planned.compute.devices) > 1 and not planned.compute.tensor_split:
        planned.compute.tensor_split = [
            float(by_index[index].total_mib) for index in planned.compute.devices
        ]
    return DeploymentProfile.model_validate(planned.model_dump(mode="json"))


class LlamaCppControlPlane:
    """Own one direct llama.cpp worker while preserving the gateway contract."""

    def __init__(
        self,
        settings: Settings,
        profiles: ProfileStore,
        deployments: DeploymentStore,
    ) -> None:
        self.settings = settings
        self.profiles = profiles
        self.deployments = deployments
        self.endpoint = WorkerEndpoint(
            settings.engine_host, settings.text_engine_port
        )
        worker_state = settings.model_state_path.with_name("text-worker.json")
        self.worker = ManagedWorker(
            provider="llama.cpp",
            endpoint=self.endpoint,
            probe=LlamaCppProbe(),
            log_path=settings.text_engine_log_path,
            state_path=worker_state,
            load_timeout_seconds=settings.model_load_timeout_seconds,
            stop_timeout_seconds=settings.engine_stop_timeout_seconds,
        )
        self.client = self.worker.client
        self.generation_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.background_task: asyncio.Task[None] | None = None
        self.background_kind: str | None = None
        self.background_target: str | None = None
        self.verified_runtime_path: str | None = None
        self.capabilities: dict[str, dict[str, Any]] | None = None
        self.chat_template: tuple[tuple[str | None, int | None], str] | None = None
        self.state = self._read_state()
        self.state.pid = None
        if self.state.phase not in {"stopped", "unloaded"}:
            self.state.phase = "external"

    def _read_state(self) -> RuntimeState:
        try:
            raw = json.loads(self.settings.model_state_path.read_text(encoding="utf-8"))
            known = RuntimeState.__dataclass_fields__
            return RuntimeState(**{key: value for key, value in raw.items() if key in known})
        except (OSError, ValueError, TypeError):
            return RuntimeState()

    def _write_state(self) -> None:
        path = self.settings.model_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(asdict(self.state), indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)

    async def close(self) -> None:
        await self.cancel_background_operation()
        await self.stop()
        await self.worker.client.aclose()

    def background_operation(self) -> dict[str, str | None] | None:
        if self.background_task is not None and not self.background_task.done():
            return {"kind": self.background_kind, "model": self.background_target}
        return None

    def start_background(
        self, kind: str, target: str | None, work: Coroutine[Any, Any, None]
    ) -> None:
        self.background_kind = kind
        self.background_target = target
        self.background_task = asyncio.create_task(work)

    async def cancel_background_operation(self) -> None:
        task = self.background_task
        if task is None or task.done():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def invalidate_profile(self, profile_id: str) -> None:
        if self.state.active_model == profile_id:
            self.state.phase = "stale"
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()

    def _runtime_status(self) -> dict[str, Any]:
        status = inspect_runtime(self.settings.runtime_dir)
        if not status["present"]:
            raise LlamaCppError(
                "no ENGRAI text runtime is active; install a runtime bundle first"
            )
        if not status["verified"]:
            raise LlamaCppError(
                f"active ENGRAI runtime failed verification: {status['error']}"
            )
        return status

    async def _components(
        self,
    ) -> tuple[dict[str, Any], Path, tuple[LlamaCppDevice, ...]]:
        try:
            status = await asyncio.to_thread(self._runtime_status)
            executable = Path(str(status["executable"]))
            devices = await asyncio.to_thread(probe_devices, executable)
        except (RuntimeManagerError, LlamaCppInventoryError) as exc:
            raise LlamaCppError(str(exc)) from exc
        self.verified_runtime_path = str(status["path"])
        return status, executable, devices

    async def resolve_deployment(self, deployment: DeploymentProfile) -> DeploymentProfile:
        status, _, devices = await self._components()
        try:
            validate_extra_arguments(deployment.advanced.extra_arguments)
        except ValueError as exc:
            raise LlamaCppError(str(exc)) from exc
        return plan_deployment(
            deployment,
            backend=str(status["backend"]),
            devices=devices,
        )

    async def compile(self, profile: RouteProfile) -> tuple[DeploymentProfile, LaunchSpec]:
        try:
            deployment = self.deployments.get(profile.id)
        except KeyError as exc:
            raise LlamaCppError(
                f"Route {profile.id!r} has no typed deployment; open and save it once"
            ) from exc
        status, executable, devices = await self._components()
        try:
            planned = plan_deployment(
                deployment,
                backend=str(status["backend"]),
                devices=devices,
            )
            adapter = LlamaCppAdapter(
                executable,
                backend=str(status["backend"]),
                devices=device_map(devices, str(status["backend"])),
                version=str(status["version"]),
            )
            launch = adapter.compile(
                _manifest_for(profile),
                planned,
                {"model": profile.model_path.resolve()},
                host=self.endpoint.host,
                port=self.endpoint.port,
            )
        except (ValueError, LlamaCppInventoryError) as exc:
            raise LlamaCppError(str(exc)) from exc
        return planned, launch

    async def engine_status(self) -> dict[str, Any]:
        worker = await self.worker.status()
        # Health/state polling must not hash a ~700 MiB CUDA binary on every
        # request.  Full verification happens before every plan or launch;
        # this read only identifies the active pointer.
        runtime = inspect_runtime(self.settings.runtime_dir, verify=False)
        return {
            **worker,
            "version": runtime.get("version"),
            "backend": runtime.get("backend"),
            "runtime_verified": runtime.get("path") == self.verified_runtime_path,
            "llm": worker.get("ready", False),
            "capabilities": self.capabilities if worker.get("owned") else None,
        }

    async def snapshot(self) -> dict[str, Any]:
        engine = await self.engine_status()
        executable = inspect_runtime(self.settings.runtime_dir, verify=False).get("executable")
        models: list[dict[str, Any]] = []
        for profile in self.profiles.list():
            item = profile.public_dict()
            try:
                deployment = self.deployments.get(profile.id)
            except KeyError:
                deployment = None
            if deployment is not None:
                item["context_tokens"] = deployment.memory.context or 0
                item["gpu_layers"] = (
                    999 if deployment.compute.gpu_layers == "all"
                    else 0 if deployment.compute.gpu_layers == "auto"
                    else deployment.compute.gpu_layers
                )
                item["deployment"] = deployment.model_dump(mode="json")
            item["active"] = profile.id == self.state.active_model and bool(engine.get("owned"))
            item["phase"] = self.state.phase if profile.id == self.state.target_model else "idle"
            models.append(item)
        return {
            "engine": engine,
            "runtime": asdict(self.state),
            "locked": self.generation_lock.locked(),
            "background": self.background_operation(),
            "models": models,
            "paths": {
                "executable": executable,
                "search_roots": [str(root) for root in self.settings.search_roots],
            },
        }

    def is_live(self, profile_id: str) -> bool:
        return (
            self.state.active_model == profile_id
            and self.state.phase == "ready"
            and self.worker.process is not None
            and self.worker.process.returncode is None
        )

    async def ensure_model(self, profile: RouteProfile) -> bool:
        if self.is_live(profile.id):
            return False
        async with self.lifecycle_lock:
            if self.is_live(profile.id):
                return False
            self.state.phase = "switching"
            self.state.target_model = profile.id
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()
            try:
                planned, launch = await self.compile(profile)
                if planned != self.deployments.get(profile.id):
                    self.deployments.save(planned)
                await self.worker.start(profile.id, launch)
                self.capabilities = effective_cache_capabilities(
                    planned, self.worker.launch_log()
                )
            except WorkerError as exc:
                self.state.phase = "error"
                self.state.error = str(exc)
                self.state.pid = None
                self.state.changed_at = time.time()
                self._write_state()
                raise LlamaCppError(str(exc)) from exc
            except Exception as exc:
                self.state.phase = "error"
                self.state.error = str(exc)
                self.state.pid = None
                self.state.changed_at = time.time()
                self._write_state()
                raise
            self.state.active_model = profile.id
            self.state.target_model = None
            self.state.phase = "ready"
            self.state.pid = self.worker.process.pid if self.worker.process else None
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()
            return True

    async def load(self, profile: RouteProfile) -> bool:
        async with self.generation_lock:
            return await self.ensure_model(profile)

    async def stop(self) -> None:
        async with self.lifecycle_lock:
            await self.worker.stop()
            self.capabilities = None
            self.state.active_model = None
            self.state.target_model = None
            self.state.pid = None
            self.state.phase = "unloaded"
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()

    async def unload(self) -> None:
        async with self.generation_lock:
            await self.stop()

    async def _chat_template(self) -> str:
        """Return the live worker's template (the GGUF's unless overridden)."""

        key = (self.state.active_model, self.state.pid)
        if self.chat_template is None or self.chat_template[0] != key:
            response = await self.client.get("/props")
            response.raise_for_status()
            self.chat_template = (key, str(response.json().get("chat_template") or ""))
        return self.chat_template[1]

    async def _rendered_chat(
        self, profile: RouteProfile, payload: dict[str, Any]
    ) -> str | None:
        """Render a chat request as a raw prompt, or None to use the native handler."""

        policy = _manifest_for(profile).prompt
        if policy is not None and policy.template == "none":
            return None
        template = await self._chat_template()
        if not template:
            return None
        try:
            effort = self.deployments.get(profile.id).generation.reasoning_effort
        except KeyError:
            effort = None
        kwargs = chat.template_kwargs(
            payload,
            thinking=bool(policy and policy.thinking),
            reasoning_effort=effort,
        )
        try:
            return chat.render_prompt(template, payload["messages"], kwargs)
        except (jinja2.TemplateError, TypeError, ValueError, KeyError, AttributeError):
            return None

    async def _generate_chat(
        self, payload: dict[str, Any], prompt: str
    ) -> tuple[httpx.Response, AsyncIterator[bytes]]:
        """Generate from a rendered prompt and answer in chat completion form."""

        body = chat.completion_request(payload, prompt)
        request = self.client.build_request("POST", "/completion", json=body)
        upstream = await self.client.send(request, stream=True)
        model = str(payload["model"])
        response = upstream
        if upstream.status_code != 200:
            source: AsyncIterator[bytes] = upstream.aiter_raw()
        elif body["stream"]:
            options = payload.get("stream_options")
            source = chat.stream_chat_completion(
                upstream.aiter_lines(),
                prompt,
                model,
                include_usage=isinstance(options, dict) and bool(options.get("include_usage")),
            )
            response = httpx.Response(
                200, headers={"content-type": "text/event-stream", "cache-control": "no-cache"}
            )
        else:

            async def single() -> AsyncIterator[bytes]:
                result = json.loads(await upstream.aread())
                yield json.dumps(chat.chat_completion(result, prompt, model)).encode("utf-8")

            source = single()
            response = httpx.Response(200, headers={"content-type": "application/json"})

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in source:
                    yield chunk
            finally:
                await upstream.aclose()
                self.generation_lock.release()

        return response, stream()

    async def forward_openai(
        self,
        method: str,
        path: str,
        raw_body: bytes,
        query: str,
        content_type: str | None,
    ) -> tuple[httpx.Response, AsyncIterator[bytes]]:
        await self.generation_lock.acquire()
        try:
            body = raw_body
            if raw_body and "json" in (content_type or ""):
                payload = json.loads(raw_body)
                requested = payload.get("model") if isinstance(payload, dict) else None
                if requested:
                    profile = self.profiles.resolve(str(requested))
                    await self.ensure_model(profile)
                    payload["model"] = profile.id
                    if (
                        method.upper() == "POST"
                        and path.strip("/") == "chat/completions"
                        and chat.supports(payload)
                    ):
                        rendered = await self._rendered_chat(profile, payload)
                        if rendered is not None:
                            return await self._generate_chat(payload, rendered)
                    body = json.dumps(payload).encode("utf-8")
            headers = {"Content-Type": content_type} if content_type else {}
            target = f"/v1/{path}" + (f"?{query}" if query else "")
            request = self.client.build_request(method, target, content=body, headers=headers)
            response = await self.client.send(request, stream=True)

            async def stream() -> AsyncIterator[bytes]:
                try:
                    async for chunk in response.aiter_raw():
                        yield chunk
                finally:
                    await response.aclose()
                    self.generation_lock.release()

            return response, stream()
        except Exception:
            if self.generation_lock.locked():
                self.generation_lock.release()
            raise


__all__ = [
    "LlamaCppControlPlane",
    "LlamaCppError",
    "RuntimeState",
    "effective_cache_capabilities",
    "plan_deployment",
]
