from pathlib import Path

import pytest
from pydantic import ValidationError

from engrai_server.domain.deployments import DeploymentProfile
from engrai_server.domain.models import ModelManifest
from engrai_server.engines.llamacpp import LlamaCppAdapter


def manifest() -> ModelManifest:
    return ModelManifest.model_validate(
        {
            "schema_version": 2,
            "id": "example-q4",
            "name": "Example Q4",
            "task": "chat",
            "format": "gguf",
            "source": {
                "provider": "huggingface",
                "repository": "example/model",
                "revision": "0123456789abcdef",
            },
            "artifacts": {"model": {"filename": "example-q4.gguf"}},
            "prompt": {"template": "jinja", "thinking": True},
        }
    )


def deployment() -> DeploymentProfile:
    return DeploymentProfile.model_validate(
        {
            "schema_version": 1,
            "id": "example-local",
            "model": "example-q4",
            "engine": "llama.cpp",
            "compute": {
                "backend": "cuda",
                "devices": [0, 1],
                "gpu_layers": "all",
                "tensor_split": [3, 1],
                "flash_attention": True,
            },
            "memory": {"context": 32768, "kv_cache": "q8_0"},
            "generation": {"default_tokens": 2048, "reasoning_effort": "low"},
        }
    )


def test_manifest_rejects_host_paths() -> None:
    payload = manifest().model_dump()
    payload["artifacts"]["model"]["filename"] = "/home/operator/model.gguf"
    with pytest.raises(ValidationError, match="portable relative paths"):
        ModelManifest.model_validate(payload)


def test_deployment_rejects_mismatched_tensor_split() -> None:
    payload = deployment().model_dump()
    payload["compute"]["tensor_split"] = [1]
    with pytest.raises(ValidationError, match="one value per selected device"):
        DeploymentProfile.model_validate(payload)


def test_advanced_arguments_cannot_override_engrai_controls() -> None:
    payload = deployment().model_dump()
    payload["advanced"] = {"extra_arguments": ["--host", "0.0.0.0"]}
    with pytest.raises(ValidationError, match="process controls"):
        DeploymentProfile.model_validate(payload)


def llama_deployment() -> DeploymentProfile:
    payload = deployment().model_dump()
    payload["engine"] = "llama.cpp"
    payload["compute"].update(
        {
            "backend": "cuda",
            "devices": [0, 1],
            "gpu_layers": "all",
            "split_mode": "layer",
            "tensor_split": [24, 12],
        }
    )
    payload["memory"].update(
        {
            "context": 32768,
            "kv_cache": "q8_0",
            "context_shift": True,
            "cache_reuse_min_tokens": 256,
            "parallel_slots": 1,
        }
    )
    return DeploymentProfile.model_validate(payload)


def test_llamacpp_adapter_compiles_an_explicit_asymmetric_gpu_plan() -> None:
    adapter = LlamaCppAdapter(
        Path("/opt/llama-server"),
        backend="cuda",
        devices={0: "CUDA0", 1: "CUDA1"},
        version="b9999",
    )
    spec = adapter.compile(
        manifest(),
        llama_deployment(),
        {"model": Path("/models/example-q4.gguf")},
        host="127.0.0.1",
        port=5002,
    )

    command = spec.command()
    assert command[:3] == (
        "/opt/llama-server",
        "--model",
        "/models/example-q4.gguf",
    )
    assert command[command.index("--device") + 1] == "CUDA0,CUDA1"
    assert command[command.index("--tensor-split") + 1] == "24.0,12.0"
    assert command[command.index("--fit") + 1] == "off"
    assert command[command.index("--ctx-size") + 1] == "32768"
    assert command[command.index("--cache-reuse") + 1] == "256"
    assert "--cache-prompt" in command
    assert "--context-shift" in command
    assert "--no-ui" in command
    assert command[-4:] == ("--n-predict", "2048", "--reasoning-effort", "low")


def test_llamacpp_adapter_rejects_unresolved_or_ambiguous_placement() -> None:
    adapter = LlamaCppAdapter(
        Path("/opt/llama-server"),
        backend="cuda",
        devices={0: "CUDA0", 1: "CUDA1"},
    )
    unresolved = llama_deployment().model_copy(deep=True)
    unresolved.compute.gpu_layers = "auto"
    with pytest.raises(ValueError, match="GPU layer count is unresolved"):
        adapter.compile(
            manifest(),
            unresolved,
            {"model": Path("/models/example.gguf")},
            host="127.0.0.1",
            port=5002,
        )

    ambiguous = llama_deployment().model_copy(deep=True)
    ambiguous.compute.devices = []
    ambiguous.compute.tensor_split = []
    with pytest.raises(ValueError, match="device selection is ambiguous"):
        adapter.compile(
            manifest(),
            ambiguous,
            {"model": Path("/models/example.gguf")},
            host="127.0.0.1",
            port=5002,
        )


def test_llamacpp_adapter_blocks_owned_flags_in_equals_form() -> None:
    direct = llama_deployment().model_copy(deep=True)
    direct.advanced.extra_arguments = ["--host=0.0.0.0"]
    adapter = LlamaCppAdapter(
        Path("/opt/llama-server"), backend="cuda", devices={0: "CUDA0", 1: "CUDA1"}
    )
    with pytest.raises(ValueError, match="ENGRAI-owned option --host"):
        adapter.compile(
            manifest(),
            direct,
            {"model": Path("/models/example.gguf")},
            host="127.0.0.1",
            port=5002,
        )


def test_llamacpp_adapter_maps_host_main_gpu_to_filtered_device_ordinal() -> None:
    direct = llama_deployment().model_copy(deep=True)
    direct.compute.devices = [1, 3]
    direct.compute.tensor_split = [12, 24]
    direct.compute.split_mode = "row"
    direct.compute.main_gpu = 3
    adapter = LlamaCppAdapter(
        Path("/opt/llama-server"), backend="cuda", devices={1: "CUDA1", 3: "CUDA3"}
    )
    spec = adapter.compile(
        manifest(),
        direct,
        {"model": Path("/models/example.gguf")},
        host="127.0.0.1",
        port=5002,
    )
    assert spec.arguments[spec.arguments.index("--main-gpu") + 1] == "1"


def test_llamacpp_adapter_enforces_a_pinned_runtime_version() -> None:
    direct = llama_deployment().model_copy(deep=True)
    direct.engine_version = "b7000"
    adapter = LlamaCppAdapter(
        Path("/opt/llama-server"),
        backend="cuda",
        devices={0: "CUDA0", 1: "CUDA1"},
        version="b6999",
    )
    with pytest.raises(ValueError, match="requires llama.cpp b7000"):
        adapter.compile(
            manifest(),
            direct,
            {"model": Path("/models/example.gguf")},
            host="127.0.0.1",
            port=5002,
        )
