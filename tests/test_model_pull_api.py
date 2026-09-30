from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture
def model_pull_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_API_KEYS", "pull-api-key")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(tmp_path / "routes"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("ENGRAI_MODEL_DOWNLOAD_DIR", str(tmp_path / "hub"))
    monkeypatch.setenv(
        "ENGRAI_MODEL_DOWNLOAD_STATE_PATH", str(tmp_path / "downloads.json")
    )
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5928")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    module.model_downloads._snapshot_fn = lambda **_kwargs: [
        SimpleNamespace(
            commit_hash="b" * 40,
            file_size=1024,
            filename="model-Q4_K_M.gguf",
            is_cached=False,
            will_download=True,
        )
    ]
    module.pull_test_session, _ = module.credentials.create_session()
    yield module
    get_settings.cache_clear()


@pytest.fixture
async def model_pull_api(model_pull_module):
    transport = httpx.ASGITransport(app=model_pull_module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
        yield client, model_pull_module


@pytest.mark.asyncio
async def test_model_pull_preview_is_admin_only(model_pull_api) -> None:
    client, _ = model_pull_api
    response = await client.post(
        "/control/model-pulls/preview",
        json={"repo_id": "owner/repo", "allow_patterns": ["*.gguf"]},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_model_pull_preview_returns_pinned_file_set(model_pull_api) -> None:
    client, module = model_pull_api
    response = await client.post(
        "/control/model-pulls/preview",
        headers={"Authorization": f"Bearer {module.pull_test_session}"},
        json={"repo_id": "owner/repo", "allow_patterns": ["*.gguf"]},
    )
    assert response.status_code == 200
    assert response.json()["commit_hash"] == "b" * 40
    assert response.json()["files"][0]["filename"] == "model-Q4_K_M.gguf"


@pytest.mark.asyncio
async def test_model_pull_api_rejects_path_like_revision(model_pull_api) -> None:
    client, module = model_pull_api
    response = await client.post(
        "/control/model-pulls/preview",
        headers={"Authorization": f"Bearer {module.pull_test_session}"},
        json={
            "repo_id": "owner/repo",
            "revision": "../../etc/passwd",
            "allow_patterns": ["*.gguf"],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["search?q=llama", "variants?repo=owner/repo"])
async def test_model_browser_is_admin_only(model_pull_api, path):
    client, _ = model_pull_api
    assert (await client.get("/control/model-browser/" + path)).status_code == 401


@pytest.mark.asyncio
async def test_model_browser_routes_and_safe_errors(model_pull_api, monkeypatch):
    client, module = model_pull_api
    headers = {"Authorization": f"Bearer {module.pull_test_session}"}
    monkeypatch.setattr(module.model_browser, "search", lambda q: {"models": [{"repo_id": q}]})
    result = await client.get("/control/model-browser/search?q=llama", headers=headers)
    assert result.json() == {"models": [{"repo_id": "llama"}]}
    assert (await client.get("/control/model-browser/search?q=", headers=headers)).status_code == 422
    def unavailable(_):
        raise RuntimeError("private transport detail")
    monkeypatch.setattr(module.model_browser, "variants", unavailable)
    result = await client.get("/control/model-browser/variants?repo=owner/repo", headers=headers)
    assert result.status_code == 502
    assert "private transport detail" not in result.text
