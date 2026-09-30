from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from engrai_server.engines.sdcpp import (
    SdCppDevice,
    SdCppError,
    SdCppProbe,
    compile_image_launch,
    parse_device_list,
)
from engrai_server.image_engine import SDCPP, ImageControlPlane, ImageEngineError
from engrai_server.runtime import MANIFEST_NAME, POLICIES, host_target, install_runtime
from engrai_server.settings import Settings

from test_image_generation import image_profile


LIST_DEVICES = """\
ggml_cuda_init: found 2 CUDA devices (Total VRAM: 36852 MiB):
  Device 0: NVIDIA RTX 4090, compute capability 8.9, VMM: yes, VRAM: 24564 MiB
  Device 1: NVIDIA RTX 3060, compute capability 8.6, VMM: yes, VRAM: 12288 MiB
CUDA0\tNVIDIA RTX 4090
CUDA1\tNVIDIA RTX 3060
CPU\tx86-64 CPU
"""
DEVICES = parse_device_list(LIST_DEVICES)


def test_device_list_keeps_only_device_records() -> None:
    assert DEVICES == (
        SdCppDevice("CUDA0", "NVIDIA RTX 4090"),
        SdCppDevice("CUDA1", "NVIDIA RTX 3060"),
        SdCppDevice("CPU", "x86-64 CPU"),
    )


def test_klein_profile_compiles_to_a_loopback_second_gpu_launch(tmp_path: Path) -> None:
    _, profile = image_profile(tmp_path)

    spec = compile_image_launch(
        profile, Path("/runtime/engrai-image-worker"), DEVICES, host="127.0.0.1", port=5003
    )
    arguments = list(spec.arguments)

    assert arguments[:4] == ["--listen-ip", "127.0.0.1", "--listen-port", "5003"]
    assert arguments[arguments.index("--diffusion-model") + 1] == str(profile.model_path)
    assert arguments[arguments.index("--llm") + 1] == str(profile.text_encoder_path)
    assert arguments[arguments.index("--vae") + 1] == str(profile.vae_path)
    assert arguments[arguments.index("--backend") + 1] == "diffusion=CUDA1,vae=CUDA1,te=CPU"
    assert arguments[arguments.index("--max-vram") + 1] == "CUDA1=7.42"
    assert {"--offload-to-cpu", "--vae-tiling", "--diffusion-fa"} <= set(arguments)


def test_gpu_ordinal_must_exist_on_this_runtime(tmp_path: Path) -> None:
    _, profile = image_profile(tmp_path)
    cpu_only = (SdCppDevice("CPU", "host"),)

    with pytest.raises(SdCppError, match="selects GPU 1"):
        compile_image_launch(profile, Path("/w"), cpu_only, host="127.0.0.1", port=5003)


def test_worker_must_stay_on_loopback(tmp_path: Path) -> None:
    _, profile = image_profile(tmp_path)

    with pytest.raises(SdCppError, match="loopback"):
        compile_image_launch(profile, Path("/w"), DEVICES, host="0.0.0.0", port=5003)


def test_probe_is_ready_only_with_capabilities() -> None:
    assert SdCppProbe().inspect(200, {"defaults": {}}).ready is True
    assert SdCppProbe().inspect(503, None).ready is False


def install_fake_image_runtime(runtime_root: Path, tmp_path: Path) -> Path:
    bundle = tmp_path / "image-bundle"
    worker = bundle / "bin" / "engrai-image-worker"
    worker.parent.mkdir(parents=True)
    worker.write_text("#!/bin/sh\nprintf 'CUDA0\\tGPU zero\\nCUDA1\\tGPU one\\nCPU\\thost\\n'\n")
    worker.chmod(0o755)
    operating_system, architecture = host_target()
    policy = POLICIES["engrai-image"]
    manifest = {
        "schema_version": 1,
        "runtime_id": "engrai-image",
        "runtime_version": "0.1.0-sdcpp-test",
        "worker_abi": 1,
        "entrypoint": "bin/engrai-image-worker",
        "os": operating_system,
        "arch": architecture,
        "backend": "cpu",
        "kernel": policy.kernel,
        "build": {
            "options": [
                *sorted(policy.required_options),
                "-DSD_CUDA=OFF",
                "-DSD_HIPBLAS=OFF",
                "-DSD_VULKAN=OFF",
                "-DSD_METAL=OFF",
            ]
        },
        "files": [
            {
                "path": "bin/engrai-image-worker",
                "sha256": hashlib.sha256(worker.read_bytes()).hexdigest(),
                "size": worker.stat().st_size,
                "mode": 0o755,
            }
        ],
    }
    (bundle / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    install_runtime(bundle, runtime_root)
    return runtime_root


def image_settings(tmp_path: Path, runtime_root: Path) -> Settings:
    return Settings(
        runtime_dir=runtime_root,
        image_command_dir=tmp_path / "image-commands",
        image_engine_port=5993,
        image_engine_log_path=tmp_path / "image.log",
        image_state_path=tmp_path / "image-state.json",
        image_result_dir=tmp_path / "results",
    )


@pytest.mark.asyncio
async def test_image_lane_launches_the_installed_image_runtime(tmp_path: Path) -> None:
    store, profile = image_profile(tmp_path)
    runtime_root = install_fake_image_runtime(tmp_path / "runtimes", tmp_path)
    control = ImageControlPlane(image_settings(tmp_path, runtime_root), store)

    command = await control._launch_command(profile)
    described = await control.describe(profile)

    assert command[0].endswith("bin/engrai-image-worker")
    assert command[command.index("--backend") + 1] == "diffusion=CUDA1,vae=CUDA1,te=CPU"
    assert described["engine"] == SDCPP
    assert described["command"].startswith(command[0])
    await control.client.aclose()


@pytest.mark.asyncio
async def test_without_an_image_runtime_there_is_no_fallback_engine(tmp_path: Path) -> None:
    store, profile = image_profile(tmp_path)
    control = ImageControlPlane(image_settings(tmp_path, tmp_path / "runtimes"), store)

    with pytest.raises(ImageEngineError, match="no ENGRAI image runtime is installed"):
        await control._launch_command(profile)
    status = await control.engine_status()
    assert (status["engine"], status["installed"]) == (SDCPP, False)
    assert (await control.describe(profile))["command"] is None
    await control.client.aclose()
