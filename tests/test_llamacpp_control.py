from pathlib import Path

from engrai_server.domain.deployments import DeploymentProfile
from engrai_server.engines.llamacpp import LlamaCppDevice, parse_device_list
from engrai_server.llamacpp import effective_cache_capabilities, plan_deployment
from engrai_server.profiles import ProfileStore


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
