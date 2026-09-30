"""Host resource sampling for the status strip.

Linux-only by design: /proc and nvidia-smi are read directly rather than
through a portability layer, because this gateway drives a local GPU and has
no reason to run anywhere else.

Parsing is separated from collection so the formats can be tested against
captured samples rather than whatever the host happens to report.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GPU_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"


@dataclass(frozen=True, slots=True)
class CpuSample:
    busy: int
    total: int


def parse_cpu_sample(stat_text: str) -> CpuSample | None:
    """Aggregate jiffies from the summary line of /proc/stat.

    Fields are user, nice, system, idle, iowait, irq, softirq, steal, ... —
    idle and iowait both count as not-busy.
    """
    for line in stat_text.splitlines():
        if not line.startswith("cpu "):
            continue
        try:
            values = [int(value) for value in line.split()[1:]]
        except ValueError:
            return None
        if len(values) < 5:
            return None
        total = sum(values)
        idle = values[3] + values[4]
        return CpuSample(busy=total - idle, total=total)
    return None


def cpu_percent_between(previous: CpuSample, current: CpuSample) -> float | None:
    """Utilisation across the interval separating two samples."""
    total_delta = current.total - previous.total
    if total_delta <= 0:
        return None
    busy_delta = current.busy - previous.busy
    return round(max(0.0, min(100.0, busy_delta * 100 / total_delta)), 1)


def parse_meminfo(meminfo_text: str) -> dict[str, Any] | None:
    """Total and available memory in MiB, plus the used percentage.

    MemAvailable is the kernel's own estimate of what a new workload could
    claim; MemFree would understate it badly, since page cache is reclaimable.
    """
    fields: dict[str, int] = {}
    for line in meminfo_text.splitlines():
        key, _, rest = line.partition(":")
        if key in {"MemTotal", "MemAvailable"}:
            try:
                fields[key] = int(rest.split()[0])
            except (IndexError, ValueError):
                return None
    if "MemTotal" not in fields or "MemAvailable" not in fields:
        return None
    total_mib = fields["MemTotal"] // 1024
    available_mib = fields["MemAvailable"] // 1024
    used_mib = total_mib - available_mib
    return {
        "total_mib": total_mib,
        "used_mib": used_mib,
        "percent": round(used_mib * 100 / total_mib, 1) if total_mib else 0.0,
    }


def parse_gpu_csv(csv_text: str) -> list[dict[str, Any]]:
    """Rows from nvidia-smi --format=csv,noheader,nounits."""
    gpus: list[dict[str, Any]] = []
    for line in csv_text.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            index, name, utilization, used, total, temperature = parts[:6]
            used_mib, total_mib = int(used), int(total)
            gpus.append(
                {
                    "index": int(index),
                    "name": name,
                    "utilization": int(utilization),
                    "used_mib": used_mib,
                    "total_mib": total_mib,
                    "percent": round(used_mib * 100 / total_mib, 1) if total_mib else 0.0,
                    "temperature": int(temperature),
                }
            )
        except ValueError:
            # A field can read [N/A] on some cards; skip rather than fail the
            # whole strip over one unsupported sensor.
            continue
    return gpus


class MetricsCollector:
    """Samples the host, holding the previous CPU reading between calls."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._previous: CpuSample | None = self._read_cpu()

    def _read_cpu(self) -> CpuSample | None:
        try:
            return parse_cpu_sample(Path("/proc/stat").read_text(encoding="utf-8"))
        except OSError:
            return None

    def cpu(self) -> dict[str, Any]:
        current = self._read_cpu()
        with self._lock:
            previous, self._previous = self._previous, current or self._previous
        if previous is None or current is None:
            return {"percent": None}
        return {"percent": cpu_percent_between(previous, current)}

    def memory(self) -> dict[str, Any] | None:
        try:
            return parse_meminfo(Path("/proc/meminfo").read_text(encoding="utf-8"))
        except OSError:
            return None

    def gpus(self) -> list[dict[str, Any]]:
        if not shutil.which("nvidia-smi"):
            return []
        try:
            output = subprocess.run(
                ["nvidia-smi", f"--query-gpu={GPU_QUERY}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            # nvidia-smi can stall while a model is loading, which is exactly
            # when this is being watched. An empty list degrades the strip
            # rather than failing the request.
            return []
        return parse_gpu_csv(output)

    def snapshot(self) -> dict[str, Any]:
        return {"cpu": self.cpu(), "memory": self.memory(), "gpus": self.gpus()}
