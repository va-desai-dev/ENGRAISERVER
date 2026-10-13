import asyncio
from pathlib import Path
from unittest.mock import Mock

import pytest

from engrai_server.domain.deployments import DeploymentProfile
from engrai_server.domain.store import DeploymentStore
from engrai_server.engines.llamacpp import LlamaCppDevice, parse_device_list
from engrai_server.llamacpp import LlamaCppControlPlane, LlamaCppError, effective_cache_capabilities, plan_deployment
from engrai_server.engines.llamacpp import LlamaCppInventoryError
from engrai_server.profiles import ProfileStore
from engrai_server.settings import Settings


@pytest.mark.asyncio
async def test_device_inventory_is_shared_and_invalidated_when_worker_changes(tmp_path, monkeypatch):
    executable = tmp_path / "worker"
    executable.write_bytes(b"worker version one")
    control = LlamaCppControlPlane(
        Settings(model_state_path=tmp_path / "state.json"),
        ProfileStore(tmp_path / "routes", [tmp_path]),
        DeploymentStore(tmp_path / "deployments"),
    )
    status = Mock(return_value={"executable": str(executable), "path": str(tmp_path)})
    inventory = Mock(return_value=(LlamaCppDevice("MTL0", "metal", 0, "Apple", 16384, 8000),))
    monkeypatch.setattr(control, "_runtime_status", status)
    monkeypatch.setattr("engrai_server.llamacpp.probe_devices", inventory)
    try:
        results = await asyncio.gather(*(control._components() for _ in range(4)))
        assert inventory.call_count == 1
        assert status.call_count == 4  # Cached devices never bypass runtime verification.
        assert all(result[2] == inventory.return_value for result in results)
        executable.write_bytes(b"replacement worker with a different size")
        await control._components()
        assert inventory.call_count == 2
    finally:
        await control.worker.client.aclose()


@pytest.mark.asyncio
async def test_failed_device_discovery_can_be_retried(tmp_path, monkeypatch):
    executable = tmp_path / "worker"
    executable.write_bytes(b"worker")
    control = LlamaCppControlPlane(
        Settings(model_state_path=tmp_path / "state.json"),
        ProfileStore(tmp_path / "routes", [tmp_path]),
        DeploymentStore(tmp_path / "deployments"),
    )
    monkeypatch.setattr(control, "_runtime_status", lambda: {"executable": str(executable), "path": str(tmp_path)})
    inventory = Mock(side_effect=[LlamaCppInventoryError("probe timed out"), ()])
    monkeypatch.setattr("engrai_server.llamacpp.probe_devices", inventory)
    try:
        with pytest.raises(LlamaCppError, match="probe timed out"):
            await control._components()
        assert (await control._components())[2] == ()
        assert inventory.call_count == 2
    finally:
        await control.worker.client.aclose()


def unresolved_deployment() -> DeploymentProfile:
    return DeploymentProfile.model_validate(
        {
            "schema_version": 1,
            "id": "gemma-local",
            "model": "gemma-local",
            "engine": "llama.cpp",
            "compute": {"backend": "auto", "gpu_layers": "auto"},
            "memory": {"context": 60000, "kv_cache": "q8_0"},
        }
    )


def test_planner_resolves_all_asymmetric_cuda_devices() -> None:
    devices = (
        LlamaCppDevice("CUDA0", "cuda", 0, "RTX 4090", 24564, 23000),
        LlamaCppDevice("CUDA1", "cuda", 1, "RTX 3060", 12288, 11000),
    )

    planned = plan_deployment(unresolved_deployment(), backend="cuda", devices=devices)

    assert planned.compute.backend == "cuda"
    assert planned.compute.devices == [0, 1]
    assert planned.compute.gpu_layers == "all"
    assert planned.compute.tensor_split == [24564.0, 12288.0]


def test_planner_resolves_pinned_metal_device_output() -> None:
    devices = parse_device_list(
        "Available devices:\n  MTL0: Apple M4 Max (28753 MiB, 28753 MiB free)\n"
    )

    planned = plan_deployment(unresolved_deployment(), backend="metal", devices=devices)

    assert planned.compute.backend == "metal"
    assert planned.compute.devices == [0]
    assert planned.compute.gpu_layers == "all"
    assert planned.compute.tensor_split == []

def test_planner_preserves_an_explicit_single_gpu_policy() -> None:
    direct = unresolved_deployment()
    direct.compute.backend = "cuda"
    direct.compute.devices = [1]
    direct.compute.gpu_layers = 42
    devices = (
        LlamaCppDevice("CUDA0", "cuda", 0, "RTX 4090", 24564, 23000),
        LlamaCppDevice("CUDA1", "cuda", 1, "RTX 3060", 12288, 11000),
    )

    planned = plan_deployment(direct, backend="cuda", devices=devices)

    assert planned.compute.devices == [1]
    assert planned.compute.gpu_layers == 42
    assert planned.compute.tensor_split == []


def test_profile_v2_persists_prompt_policy_for_direct_worker(tmp_path: Path) -> None:
    model = tmp_path / "gemma.gguf"
    model.write_bytes(b"GGUF")
    store = ProfileStore(tmp_path / "commands", [tmp_path])

    saved = store.save(
        "gemma-local",
        name="Gemma",
        model_path=str(model),
        prompt={"template": "jinja", "adapter": "auto", "thinking": True},
    )

    assert saved.prompt is not None
    assert saved.prompt.template == "jinja"
    assert saved.prompt.thinking is True
    assert saved.public_dict()["prompt"] == {
        "template": "jinja",
        "adapter": "auto",
        "thinking": True,
    }


def test_gemma_reports_requested_features_separately_from_effective_support() -> None:
    deployment = unresolved_deployment()
    telemetry = """
    KV cache shifting is not supported for this model, disabling context shift
    cache_reuse is not supported for this model
    """

    capabilities = effective_cache_capabilities(deployment, telemetry)

    assert capabilities["exact_prefix_cache"]["effective"] is True
    assert capabilities["context_shift"] == {
        "requested": True,
        "effective": False,
        "detail": "rejected by the loaded model architecture",
    }
    assert capabilities["chunk_reuse"]["effective"] is False
