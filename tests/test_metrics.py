from __future__ import annotations

from engrai_server.metrics import (
    CpuSample,
    cpu_percent_between,
    parse_cpu_sample,
    parse_gpu_csv,
    parse_meminfo,
)


PROC_STAT = """cpu  7745017 12144 3281329 7518818034 137503 1367328 1310890 0 0 0
cpu0 242030 379 102541 234931806 4297 42729 40965 0 0 0
intr 1234567
"""

MEMINFO = """MemTotal:       65633292 kB
MemFree:         2110048 kB
MemAvailable:   56672436 kB
Buffers:            1024 kB
"""

NVIDIA_CSV = """0, NVIDIA GeForce RTX 4090, 0, 22168, 24564, 40
1, NVIDIA GeForce RTX 3060, 0, 2, 12288, 33
"""


def test_cpu_sample_treats_iowait_as_idle() -> None:
    sample = parse_cpu_sample(PROC_STAT)
    assert sample is not None
    total = 7745017 + 12144 + 3281329 + 7518818034 + 137503 + 1367328 + 1310890
    assert sample.total == total
    # idle + iowait are both not-busy.
    assert sample.busy == total - (7518818034 + 137503)


def test_cpu_percent_between_samples() -> None:
    previous = CpuSample(busy=100, total=1000)
    current = CpuSample(busy=350, total=2000)
    assert cpu_percent_between(previous, current) == 25.0


def test_cpu_percent_needs_elapsed_time() -> None:
    same = CpuSample(busy=100, total=1000)
    assert cpu_percent_between(same, same) is None


def test_cpu_percent_is_clamped() -> None:
    # Counters can appear to move backwards across a suspend.
    weird = cpu_percent_between(CpuSample(busy=500, total=1000), CpuSample(busy=100, total=2000))
    assert weird == 0.0


def test_meminfo_uses_available_not_free() -> None:
    memory = parse_meminfo(MEMINFO)
    assert memory is not None
    # Page cache is reclaimable, so MemFree would overstate pressure badly.
    assert memory["total_mib"] == 65633292 // 1024
    # Each value is converted before subtracting, so used + available equals
    # total exactly in the units displayed; subtracting first would leave the
    # bar and the numbers disagreeing by a rounding step.
    assert memory["used_mib"] == 65633292 // 1024 - 56672436 // 1024
    assert memory["used_mib"] + 56672436 // 1024 == memory["total_mib"]
    assert memory["percent"] == 13.7


def test_meminfo_rejects_incomplete_input() -> None:
    assert parse_meminfo("MemTotal: 100 kB\n") is None


def test_gpu_csv_parsing() -> None:
    gpus = parse_gpu_csv(NVIDIA_CSV)
    assert len(gpus) == 2
    assert gpus[0]["name"] == "NVIDIA GeForce RTX 4090"
    assert gpus[0]["used_mib"] == 22168
    assert gpus[0]["percent"] == 90.2
    assert gpus[1]["temperature"] == 33


def test_gpu_csv_skips_unsupported_sensors() -> None:
    # Some cards report [N/A] for a field; one bad row must not lose the rest.
    text = "0, Card A, [N/A], 100, 200, 40\n1, Card B, 10, 50, 200, 35\n"
    gpus = parse_gpu_csv(text)
    assert [gpu["name"] for gpu in gpus] == ["Card B"]


def test_gpu_csv_handles_no_devices() -> None:
    assert parse_gpu_csv("") == []
