from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from .engines.sdcpp import SdCppDevice, SdCppError, SdCppProbe, compile_image_launch, probe_devices
from .image_profiles import ImageProfile, ImageProfileStore
from .runtime import IMAGE_RUNTIME, inspect_runtime
from .settings import Settings

SDCPP = "stable-diffusion.cpp"


class ImageEngineError(RuntimeError):
    pass


@dataclass(slots=True)
class ImageRuntimeState:
    active_model: str | None = None
    phase: str = "stopped"
    target_model: str | None = None
    pid: int | None = None
    error: str | None = None
    changed_at: float = 0.0


def _number(payload: dict[str, Any], key: str, default: int | float) -> int | float:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImageEngineError(f"{key} must be a number")
    return value


def translate_openai_request(
    payload: dict[str, Any],
    seed: int,
    *,
    default_steps: int = 4,
    default_cfg_scale: float = 1.0,
    default_sampler: str = "euler",
    default_scheduler: str = "",
) -> dict[str, Any]:
    """Translate one OpenAI image request into the worker's A1111 dialect."""
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ImageEngineError("prompt is required")

    size = payload.get("size", "1024x1024")
    if not isinstance(size, str) or not re.fullmatch(r"\d{2,4}x\d{2,4}", size):
        raise ImageEngineError("size must look like 1024x1024")
    width, height = (int(value) for value in size.split("x", 1))
    for name, value in (("width", width), ("height", height)):
        if value < 64 or value > 3072 or value % 64:
            raise ImageEngineError(f"{name} must be a multiple of 64 from 64 to 3072")

    negative = payload.get("negative_prompt", "")
    if not isinstance(negative, str):
        raise ImageEngineError("negative_prompt must be text")
    steps = int(_number(payload, "steps", default_steps))
    cfg_scale = float(
        payload.get("cfg_scale", payload.get("guidance_scale", default_cfg_scale))
    )
    if not 1 <= steps <= 150:
        raise ImageEngineError("steps must be from 1 to 150")
    if not 0 <= cfg_scale <= 50:
        raise ImageEngineError("cfg_scale must be from 0 to 50")

    translated: dict[str, Any] = {
        "prompt": prompt.strip(),
        "negative_prompt": negative,
        "width": width,
        "height": height,
        "steps": steps,
        "cfg_scale": cfg_scale,
        "seed": seed,
    }
    sampler = payload.get("sampler_name", default_sampler)
    scheduler = payload.get("scheduler", default_scheduler)
    if sampler:
        if not isinstance(sampler, str):
            raise ImageEngineError("sampler_name must be text")
        translated["sampler_name"] = sampler
    if scheduler:
        if not isinstance(scheduler, str):
            raise ImageEngineError("scheduler must be text")
        translated["scheduler"] = scheduler
    return translated


class ImageControlPlane:
    """Own one image worker process and its independent request lane.

    The worker is always the verified, ENGRAI-built stable-diffusion.cpp
    runtime (``engrai-image``). There is no fallback engine: without an
    installed image runtime, image requests fail with install instructions.
    """

    def __init__(self, settings: Settings, profiles: ImageProfileStore):
        self.settings = settings
        self.profiles = profiles
        self.client = httpx.AsyncClient(
            base_url=settings.image_engine_base_url,
            timeout=httpx.Timeout(30.0, read=None),
        )
        self.generation_lock = asyncio.Lock()
        self.lifecycle_lock = asyncio.Lock()
        self.process: asyncio.subprocess.Process | None = None
        self.log_handle: BinaryIO | None = None
        self.sdcpp_devices: tuple[Path, tuple[SdCppDevice, ...]] | None = None
        self.state = self._read_state()
        self.state.pid = None
        if self.state.phase not in {"stopped", "unloaded"}:
            self.state.phase = "external"

    def _read_state(self) -> ImageRuntimeState:
        try:
            raw = json.loads(self.settings.image_state_path.read_text(encoding="utf-8"))
            known = ImageRuntimeState.__dataclass_fields__
            return ImageRuntimeState(**{key: value for key, value in raw.items() if key in known})
        except (OSError, ValueError, TypeError):
            return ImageRuntimeState()

    def _write_state(self) -> None:
        path = self.settings.image_state_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(self.state), indent=2), encoding="utf-8")
        os.replace(temporary, path)

    def _owned_running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def is_live(self, profile_id: str) -> bool:
        return (
            self.state.active_model == profile_id
            and self.state.phase == "ready"
            and self._owned_running()
        )

    def invalidate_profile(self, profile_id: str) -> None:
        if self.state.active_model == profile_id:
            self.state.phase = "stale"
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()

    def _image_runtime(self, *, verify: bool = True) -> tuple[Path, str]:
        """Return the installed image runtime's executable and version.

        Launching verifies every byte of the bundle. Status polling only reads
        the manifest: hashing a ~700 MB worker on every UI poll is too slow.
        """

        status = inspect_runtime(self.settings.runtime_dir, IMAGE_RUNTIME, verify=verify)
        if not status["present"]:
            raise ImageEngineError(
                "no ENGRAI image runtime is installed; install one with "
                "`engrai-server runtime install <engrai-image bundle>`"
            )
        if status["error"] or (verify and not status["verified"]):
            raise ImageEngineError(
                f"installed ENGRAI image runtime failed verification: {status['error']}"
            )
        return Path(status["executable"]), str(status["version"])

    async def _sdcpp_devices(self, executable: Path) -> tuple[SdCppDevice, ...]:
        if self.sdcpp_devices is None or self.sdcpp_devices[0] != executable:
            try:
                self.sdcpp_devices = (executable, await probe_devices(executable))
            except (OSError, SdCppError) as exc:
                raise ImageEngineError(str(exc)) from exc
        return self.sdcpp_devices[1]

    async def _launch_command(self, profile: ImageProfile, *, verify: bool = True) -> list[str]:
        executable, _ = self._image_runtime(verify=verify)
        try:
            spec = compile_image_launch(
                profile,
                executable,
                await self._sdcpp_devices(executable),
                host=self.settings.engine_host,
                port=self.settings.image_engine_port,
            )
        except SdCppError as exc:
            raise ImageEngineError(str(exc)) from exc
        return list(spec.command())

    async def _probe(self) -> bool:
        """Ask the worker whether it can generate."""

        response = await self.client.get(SdCppProbe.path, timeout=3.0)
        payload = response.json() if response.is_success else None
        return SdCppProbe().inspect(response.status_code, payload).ready

    async def engine_status(self) -> dict[str, Any]:
        base: dict[str, Any] = {
            "engine": SDCPP,
            "owned": self._owned_running(),
            "pid": self.process.pid if self._owned_running() else None,
        }
        try:
            base["version"] = self._image_runtime(verify=False)[1]
        except ImageEngineError as exc:
            return {**base, "installed": False, "reachable": False, "error": str(exc)}
        try:
            ready = await self._probe()
            return {**base, "installed": True, "reachable": True, "txt2img": ready}
        except (httpx.HTTPError, ValueError) as exc:
            return {**base, "installed": True, "reachable": False, "error": str(exc)}

    async def describe(self, profile: ImageProfile) -> dict[str, Any]:
        """Public profile view, with the launch command that would really run.

        The command needs the runtime's device list, probed once and cached;
        it is ``None`` when no image runtime is installed.
        """

        command = None
        with contextlib.suppress(ImageEngineError):
            command = await self._launch_command(profile, verify=False)
        return {**profile.public_dict(command), "engine": SDCPP}

    async def snapshot(self) -> dict[str, Any]:
        engine = await self.engine_status()
        models = []
        for profile in self.profiles.list():
            item = await self.describe(profile)
            item["active"] = profile.id == self.state.active_model and engine.get("owned")
            item["phase"] = self.state.phase if profile.id == self.state.target_model else "idle"
            models.append(item)
        return {
            "image_engine": engine,
            "image_runtime": asdict(self.state),
            "image_locked": self.generation_lock.locked(),
            "image_models": models,
        }

    async def _port_is_open(self) -> bool:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.settings.engine_host,
                    self.settings.image_engine_port,
                ),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (OSError, TimeoutError):
            return False

    def _log_tail(self, limit: int = 3000) -> str:
        try:
            with self.settings.image_engine_log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                handle.seek(max(0, handle.tell() - limit))
                return handle.read().decode("utf-8", errors="replace").strip()
        except OSError:
            return ""

    async def _wait_until_ready(self) -> None:
        deadline = time.monotonic() + self.settings.model_load_timeout_seconds
        last_error = "Image worker did not become ready"
        while time.monotonic() < deadline:
            if self.process and self.process.returncode is not None:
                tail = self._log_tail()
                raise ImageEngineError(
                    f"Image worker exited with status {self.process.returncode}"
                    + (f":\n{tail}" if tail else "")
                )
            try:
                if await self._probe():
                    return
            except (httpx.HTTPError, ValueError) as exc:
                last_error = str(exc)
            await asyncio.sleep(0.75)
        raise ImageEngineError(last_error)

    async def _stop_owned_process(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            self.process = None
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(
                process.wait(), timeout=self.settings.engine_stop_timeout_seconds
            )
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
        self.process = None
        if self.log_handle:
            self.log_handle.close()
            self.log_handle = None

    async def _start_profile(self, profile: ImageProfile) -> None:
        command = await self._launch_command(profile)
        if await self._port_is_open() and not self._owned_running():
            raise ImageEngineError(
                f"{self.settings.engine_host}:"
                f"{self.settings.image_engine_port} is already occupied"
            )
        await self._stop_owned_process()
        self.settings.image_engine_log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.settings.image_engine_log_path.open("ab", buffering=0)
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=self.log_handle,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        self.state.pid = self.process.pid
        self._write_state()
        await self._wait_until_ready()

    async def ensure_model(self, profile: ImageProfile) -> bool:
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
                await self._start_profile(profile)
            except Exception as exc:
                await self._stop_owned_process()
                self.state.phase = "error"
                self.state.error = str(exc)
                self.state.pid = None
                self.state.changed_at = time.time()
                self._write_state()
                raise
            self.state.active_model = profile.id
            self.state.target_model = None
            self.state.phase = "ready"
            self.state.error = None
            self.state.changed_at = time.time()
            self._write_state()
            return True

    async def load(self, profile: ImageProfile) -> bool:
        async with self.generation_lock:
            return await self.ensure_model(profile)

    async def unload(self) -> None:
        async with self.generation_lock:
            async with self.lifecycle_lock:
                await self._stop_owned_process()
                self.state = ImageRuntimeState(phase="unloaded", changed_at=time.time())
                self._write_state()

    async def close(self) -> None:
        await self.unload()
        await self.client.aclose()

    def choose_profile(self, requested: object) -> ImageProfile:
        if isinstance(requested, str) and requested:
            return self.profiles.resolve(requested)
        if self.state.active_model:
            return self.profiles.resolve(self.state.active_model)
        available = [profile for profile in self.profiles.list() if profile.routable]
        if len(available) == 1:
            return available[0]
        raise ImageEngineError("model is required when more than one image model is configured")

    async def generate(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        profile = self.choose_profile(payload.get("model"))
        n = int(_number(payload, "n", 1))
        if not 1 <= n <= 10:
            raise ImageEngineError("n must be from 1 to 10")
        requested_seed = int(_number(payload, "seed", -1))

        async with self.generation_lock:
            await self.ensure_model(profile)
            generated: list[dict[str, Any]] = []
            for index in range(n):
                seed = requested_seed + index if requested_seed >= 0 else -1
                upstream = translate_openai_request(
                    payload,
                    seed,
                    default_steps=profile.default_steps,
                    default_cfg_scale=profile.default_cfg_scale,
                    default_sampler=profile.default_sampler,
                    default_scheduler=profile.default_scheduler,
                )
                response = await self.client.post("/sdapi/v1/txt2img", json=upstream)
                response.raise_for_status()
                result = response.json()
                images = result.get("images") if isinstance(result, dict) else None
                if not isinstance(images, list) or not images or not isinstance(images[0], str):
                    raise ImageEngineError("image worker returned no image")
                actual_seed = seed
                info = result.get("info")
                if isinstance(info, str):
                    with contextlib.suppress(ValueError, TypeError):
                        actual_seed = int(json.loads(info).get("seed", seed))
                generated.append(
                    {
                        "b64_json": images[0],
                        "seed": actual_seed,
                        "model": profile.id,
                    }
                )
            return generated
