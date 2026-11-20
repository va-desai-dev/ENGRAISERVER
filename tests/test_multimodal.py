from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from engrai_server.domain.deployments import DeploymentProfile
from engrai_server.domain.store import DeploymentStore
from engrai_server.engines.llamacpp import LlamaCppDevice
from engrai_server.llamacpp import LlamaCppControlPlane, LlamaCppError
from engrai_server.profiles import ProfileStore
from engrai_server.settings import Settings


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["cpu", "cuda"])
async def test_saved_projector_compiles_with_cache_symlinks(tmp_path, monkeypatch, backend):
    blobs = tmp_path / "blobs"
    blobs.mkdir()
    model_blob = blobs / "model-hash"
    projector_blob = blobs / "projector-hash"
    model_blob.write_bytes(b"GGUF")
    projector_blob.write_bytes(b"GGUF")
    model = tmp_path / "model.gguf"
    projector = tmp_path / "mmproj.gguf"
    model.symlink_to(model_blob)
    projector.symlink_to(projector_blob)
    profiles = ProfileStore(tmp_path / "routes", [])
    profiles.save("vision", name="Vision", model_path=str(model), mmproj_path=str(projector))
    deployments = DeploymentStore(tmp_path / "deployments")
    deployments.save(DeploymentProfile.model_validate({
        "id": "vision", "model": "vision", "engine": "llama.cpp",
        "compute": {"backend": backend}, "memory": {"context": 4096},
    }))
    control = LlamaCppControlPlane(Settings(model_state_path=tmp_path / "state.json"), profiles, deployments)
    devices = () if backend == "cpu" else (LlamaCppDevice("CUDA0", "cuda", 0, "Test GPU", 16384, 8000),)
    monkeypatch.setattr(control, "_components", AsyncMock(return_value=(
        {"backend": backend, "version": "test"}, Path("/worker"), devices,
    )))
    try:
        _, launch = await control.compile(profiles.resolve("vision"))
        assert launch.arguments[launch.arguments.index("--model") + 1] == str(model_blob)
        assert launch.arguments[launch.arguments.index("--mmproj") + 1] == str(projector_blob)
        assert ("--no-mmproj-offload" in launch.arguments) == (backend == "cpu")
    finally:
        await control.client.aclose()


@pytest.mark.asyncio
async def test_missing_projector_fails_before_runtime_probe(tmp_path, monkeypatch):
    model = tmp_path / "model.gguf"
    model.write_bytes(b"GGUF")
    profiles = ProfileStore(tmp_path / "routes", [])
    profile = profiles.save("vision", name="Vision", model_path=str(model),
                            mmproj_path=str(tmp_path / "missing.gguf"))
    control = LlamaCppControlPlane(Settings(model_state_path=tmp_path / "state.json"), profiles,
                                   DeploymentStore(tmp_path / "deployments"))
    probe = AsyncMock()
    monkeypatch.setattr(control, "_components", probe)
    try:
        with pytest.raises(LlamaCppError, match="missing.gguf"):
            await control.compile(profile)
        probe.assert_not_awaited()
    finally:
        await control.client.aclose()
