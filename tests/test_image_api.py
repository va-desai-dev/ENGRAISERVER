from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from engrai_server.image_profiles import ImageProfileStore


@pytest.fixture
def image_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    commands = tmp_path / "commands"
    image_commands = tmp_path / "image-commands"
    for name in ("flux.gguf", "qwen.gguf", "flux2vae.safetensors"):
        (tmp_path / name).write_bytes(b"weights")
    ImageProfileStore(image_commands, []).save(
        "flux-test",
        name="FLUX Test",
        model_path=str(tmp_path / "flux.gguf"),
        text_encoder_path=str(tmp_path / "qwen.gguf"),
        vae_path=str(tmp_path / "flux2vae.safetensors"),
    )
    monkeypatch.setenv("CONTROL_API_KEYS", "image-api-key")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(commands))
    monkeypatch.setenv("ENGRAI_IMAGE_COMMAND_DIR", str(image_commands))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text.log"))
    monkeypatch.setenv("ENGRAI_IMAGE_ENGINE_LOG", str(tmp_path / "image.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("IMAGE_STATE_PATH", str(tmp_path / "image-runtime.json"))
    monkeypatch.setenv("IMAGE_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5918")
    monkeypatch.setenv("ENGRAI_IMAGE_ENGINE_PORT", "5919")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    module.images.generate = AsyncMock(
        return_value=[{"b64_json": "aW1hZ2U=", "seed": 123, "model": "flux-test"}]
    )
    with TestClient(module.app) as client:
        client.headers["Authorization"] = "Bearer image-api-key"
        yield client, module
    get_settings.cache_clear()


def test_openai_image_endpoint_returns_base64_and_extended_seed(image_api) -> None:
    client, module = image_api
    response = client.post(
        "/v1/images/generations",
        json={"model": "flux-test", "prompt": "a red teapot", "seed": 123},
    )
    assert response.status_code == 200
    item = response.json()["data"][0]
    assert item["b64_json"] == "aW1hZ2U="
    assert item["url"].endswith(".png")
    assert item["revised_prompt"] == "a red teapot"
    assert item["seed"] == 123
    assert item["model"] == "flux-test"
    module.images.generate.assert_awaited_once()


def test_openai_url_output_is_short_lived_and_fetchable_without_auth(image_api) -> None:
    client, _ = image_api
    response = client.post(
        "/v1/images/generations",
        json={"model": "flux-test", "prompt": "a red teapot", "response_format": "url"},
    )
    assert response.status_code == 200
    url = response.json()["data"][0]["url"]
    client.headers.clear()
    fetched = client.get(url)
    assert fetched.status_code == 200
    assert fetched.content == b"image"


def test_result_url_uses_the_external_https_proxy_scheme(image_api) -> None:
    client, _ = image_api
    response = client.post(
        "/v1/images/generations",
        headers={"Host": "llm.example.com", "X-Forwarded-Proto": "https"},
        json={"model": "flux-test", "prompt": "a red teapot"},
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["url"].startswith(
        "https://llm.example.com/v1/images/results/"
    )


def test_model_catalog_includes_image_profiles(image_api) -> None:
    client, _ = image_api
    response = client.get("/v1/models")
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()["data"]}
    assert by_id["flux-test"]["owned_by"] == "self-image"


def test_data_url_browser_preflight_is_allowed(image_api) -> None:
    client, _ = image_api
    client.headers.clear()
    response = client.options(
        "/v1/images/generations",
        headers={
            "Origin": "null",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "*"
    assert "Authorization" in response.headers["access-control-allow-headers"]


def test_observed_non_v1_wrapper_path_is_a_compatible_alias(image_api) -> None:
    client, _ = image_api
    response = client.post(
        "/images/generations",
        json={"model": "flux-test", "prompt": "a red teapot"},
    )
    assert response.status_code == 200
    assert response.json()["data"][0]["url"].endswith(".png")
