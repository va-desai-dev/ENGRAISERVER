from pathlib import Path

from engrai_server.domain.deployments import DeploymentProfile
from engrai_server.domain.store import DeploymentStore


def test_deployment_store_is_atomic_private_xdg_data(tmp_path: Path) -> None:
    store = DeploymentStore(tmp_path / "deployments")
    profile = DeploymentProfile.model_validate(
        {
            "schema_version": 1,
            "id": "demo-local",
            "model": "demo",
            "engine": "llama.cpp",
            "compute": {"backend": "metal", "gpu_layers": "all"},
        }
    )

    saved = store.save(profile)

    assert saved == profile
    assert store.get("demo-local") == profile
    assert store.list() == [profile]
    assert (tmp_path / "deployments/demo-local.json").stat().st_mode & 0o777 == 0o600
    assert not list((tmp_path / "deployments").glob("*.tmp"))
