from pathlib import Path

import pytest

from engrai_server.engines.llamacpp import (
    LlamaCppInventoryError,
    device_map,
    parse_device_list,
    probe_devices,
)


SAMPLE = """ggml_cuda_init: found 2 CUDA devices:
  Device 0: NVIDIA RTX 4090, compute capability 8.9, VMM: yes
  Device 1: NVIDIA RTX 3060, compute capability 8.6, VMM: yes
Available devices:
  CUDA0: NVIDIA RTX 4090 (24564 MiB, 23892 MiB free)
  CUDA1: NVIDIA RTX 3060 (12288 MiB, 11744 MiB free)
"""


def test_parse_device_list_preserves_asymmetric_memory_and_engine_ids() -> None:
    devices = parse_device_list(SAMPLE)

    assert [(device.engine_id, device.free_mib) for device in devices] == [
        ("CUDA0", 23892),
        ("CUDA1", 11744),
    ]
    assert device_map(devices, "cuda") == {0: "CUDA0", 1: "CUDA1"}


def test_parse_device_list_distinguishes_rocm_and_vulkan_views() -> None:
    devices = parse_device_list(
        """Available devices:
  ROCm0: Radeon RX 7900 XTX (24560 MiB, 24524 MiB free)
  Vulkan0: AMD Radeon RX 7900 XTX (24560 MiB, 24000 MiB free)
"""
    )
    assert [device.backend for device in devices] == ["rocm", "vulkan"]
    assert device_map(devices, "rocm") == {0: "ROCm0"}
    assert device_map(devices, "vulkan") == {0: "Vulkan0"}


@pytest.mark.parametrize("engine_id", ["MTL0", "Metal0"])
def test_probe_devices_executes_the_selected_binary(tmp_path: Path, engine_id: str) -> None:    executable = tmp_path / "llama-server"
    executable.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'Available devices:' "
               f"'  {engine_id}: Apple M4 Max (49152 MiB, 40000 MiB free)'\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)

    devices = probe_devices(executable)
   assert devices[0].engine_id == engine_id    
   assert devices[0].backend == "metal"



def test_pinned_metal_output_ignores_blas_and_preserves_device_id() -> None:
    devices = parse_device_list(
        """0.00.000.073 I srv  llama_server: initializing ...
Available devices:
  MTL0: Apple M4 Max (28753 MiB, 28753 MiB free)
  BLAS: Accelerate (0 MiB, 0 MiB free)
"""
    )

    assert len(devices) == 1
    assert devices[0].backend == "metal"
    assert devices[0].name == "Apple M4 Max"
    assert devices[0].total_mib == devices[0].free_mib == 28753
    assert device_map(devices, "metal") == {0: "MTL0"}


def test_probe_devices_reports_binary_failure(tmp_path: Path) -> None:
    executable = tmp_path / "llama-server"
    executable.write_text("#!/bin/sh\necho broken >&2\nexit 7\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(LlamaCppInventoryError, match="exited with 7: broken"):
        probe_devices(executable)
