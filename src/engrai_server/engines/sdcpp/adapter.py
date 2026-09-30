"""Compile ENGRAI image profiles into stable-diffusion.cpp ``sd-server`` launches.

The worker listens on loopback only and never serves its upstream web UI (the
runtime is built without it). Devices are addressed by the names the worker
itself reports through ``--list-devices``, so an image profile's GPU ordinal
always resolves against this exact runtime build and host.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..protocol import LaunchSpec
from ..worker import ProbeResult

if TYPE_CHECKING:
    from ...image_profiles import ImageProfile


_DEVICE_LINE = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)\t(?P<description>.+)$")


class SdCppError(RuntimeError):
    """An image profile could not be compiled for, or probed on, sd-server."""


@dataclass(frozen=True, slots=True)
class SdCppDevice:
    name: str
    description: str

    @property
    def accelerator(self) -> bool:
        return self.name.upper() != "CPU"


def parse_device_list(output: str) -> tuple[SdCppDevice, ...]:
    """Parse ``sd-server --list-devices``: one ``name<TAB>description`` per line.

    ggml prints its own initialization lines first; only tab-separated device
    records are kept.
    """

    devices = []
    for line in output.splitlines():
        match = _DEVICE_LINE.match(line.strip("\r"))
        if match:
            devices.append(SdCppDevice(match["name"], match["description"].strip()))
    return tuple(devices)


async def probe_devices(executable: Path, timeout: float = 30.0) -> tuple[SdCppDevice, ...]:
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--list-devices",
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise SdCppError("image runtime did not list its devices in time") from exc
    if process.returncode:
        raise SdCppError(
            f"image runtime --list-devices exited with status {process.returncode}"
        )
    devices = parse_device_list(output.decode("utf-8", errors="replace"))
    if not devices:
        raise SdCppError("image runtime reported no devices")
    return devices


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def compile_image_launch(
    profile: ImageProfile,
    executable: Path,
    devices: tuple[SdCppDevice, ...],
    *,
    host: str,
    port: int,
) -> LaunchSpec:
    """Map one image profile onto sd-server arguments.

    The profile's ``gpu`` is an ordinal among the accelerators the runtime
    reports, matching the CUDA ordinal the old compatibility worker used. The
    diffusion model and VAE run there; the text encoder stays on the CPU, as
    in the reference FLUX.2 Klein layout.
    """

    if not _is_loopback(host):
        raise SdCppError("engine workers must bind to a loopback address")
    if not 1 <= port <= 65535:
        raise SdCppError("engine worker port must be between 1 and 65535")
    accelerators = [device for device in devices if device.accelerator]
    if not 0 <= profile.gpu < len(accelerators):
        reported = ", ".join(device.name for device in accelerators) or "none"
        raise SdCppError(
            f"image profile selects GPU {profile.gpu}, but the image runtime reports: {reported}"
        )
    device = accelerators[profile.gpu].name
    for role, path in (
        ("diffusion model", profile.model_path),
        ("text encoder", profile.text_encoder_path),
        ("VAE", profile.vae_path),
    ):
        if not path.is_absolute():
            raise SdCppError(f"image {role} path must be absolute: {path}")

    arguments = [
        "--listen-ip",
        host,
        "--listen-port",
        str(port),
        "--diffusion-model",
        str(profile.model_path),
        "--llm",
        str(profile.text_encoder_path),
        "--vae",
        str(profile.vae_path),
        "--backend",
        f"diffusion={device},vae={device},te=CPU",
        "--diffusion-fa",
    ]
    if profile.vram_limit_mib:
        arguments.extend(("--max-vram", f"{device}={profile.vram_limit_mib / 1024:.2f}"))
    if profile.offload_to_cpu:
        arguments.append("--offload-to-cpu")
    if profile.tiled_vae:
        arguments.append("--vae-tiling")
    return LaunchSpec(executable, tuple(arguments), {})


class SdCppProbe:
    path = "/sdcpp/v1/capabilities"

    def inspect(self, status_code: int, payload: Any) -> ProbeResult:
        if status_code == 200 and isinstance(payload, dict) and "defaults" in payload:
            return ProbeResult(ready=True)
        return ProbeResult(ready=False, detail=f"sd-server capabilities returned HTTP {status_code}")
