"""Discover the devices a particular llama-server binary can actually use."""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


_DEVICE = re.compile(
    r"^\s*(?P<id>(?P<backend>CUDA|ROCm|MTL|Metal|Vulkan)(?P<index>\d+)):\s+"
    r"(?P<name>.+?)\s+\((?P<total>\d+) MiB,\s+(?P<free>\d+) MiB free\)\s*$"
)


class LlamaCppInventoryError(RuntimeError):
    """llama-server device discovery failed or produced unusable output."""


@dataclass(frozen=True, slots=True)
class LlamaCppDevice:
    engine_id: str
    backend: str
    index: int
    name: str
    total_mib: int
    free_mib: int


def parse_device_list(output: str) -> tuple[LlamaCppDevice, ...]:
    """Parse only the stable ``Available devices`` records from llama.cpp."""

    devices: list[LlamaCppDevice] = []
    in_devices = False
    for line in output.splitlines():
        if line.strip() == "Available devices:":
            in_devices = True
            continue
        if not in_devices:
            continue
        match = _DEVICE.match(line)
        if not match:
            continue
        devices.append(
            LlamaCppDevice(
                engine_id=match.group("id"),
                # The pinned Metal worker reports MTL0, while deployments
                # call the backend Metal. Preserve the worker's device ID.
                backend={"MTL": "metal"}.get(
                    match.group("backend"), match.group("backend").lower()
                ),
                index=int(match.group("index")),
                name=match.group("name"),
                total_mib=int(match.group("total")),
                free_mib=int(match.group("free")),
            )
        )
    return tuple(devices)


def probe_devices(executable: Path, *, timeout: float = 15.0) -> tuple[LlamaCppDevice, ...]:
    if not executable.is_file():
        raise LlamaCppInventoryError(f"llama-server executable not found: {executable}")
    try:
        completed = subprocess.run(
            [str(executable), "--list-devices"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LlamaCppInventoryError(f"could not inspect llama-server devices: {exc}") from exc
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    if completed.returncode != 0:
        detail = output.strip().splitlines()[-1] if output.strip() else "no diagnostic output"
        raise LlamaCppInventoryError(
            f"llama-server --list-devices exited with {completed.returncode}: {detail}"
        )
    return parse_device_list(output)


def device_map(
    devices: tuple[LlamaCppDevice, ...], backend: str
) -> dict[int, str]:
    """Return adapter input for one backend, rejecting duplicate indices."""

    selected = [device for device in devices if device.backend == backend]
    mapping = {device.index: device.engine_id for device in selected}
    if len(mapping) != len(selected):
        raise LlamaCppInventoryError(f"duplicate {backend} device indices in llama.cpp output")
    return mapping
