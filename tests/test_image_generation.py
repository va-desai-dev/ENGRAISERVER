from __future__ import annotations

import json
import socket
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from engrai_server.image_engine import ImageControlPlane, ImageEngineError, translate_openai_request
from engrai_server.image_profiles import ImageProfileStore
from engrai_server.settings import Settings


def image_profile(tmp_path: Path):
    model = tmp_path / "flux-klein-Q4_K_M.gguf"
    encoder = tmp_path / "Qwen3-8B-Q4_K_M.gguf"
    vae = tmp_path / "flux2vae.safetensors"
    for path in (model, encoder, vae):
        path.write_bytes(b"weights")
    store = ImageProfileStore(tmp_path / "image-commands", [tmp_path])
    profile = store.save(
        "flux-klein",
        name="FLUX Klein",
        model_path=str(model),
        text_encoder_path=str(encoder),
        vae_path=str(vae),
        gpu=1,
        vram_limit_mib=7600,
        tiled_vae=768,
        offload_to_cpu=True,
    )
    return store, profile


def test_image_worker_defaults_follow_a_relocated_text_worker(tmp_path: Path) -> None:
    settings = Settings(
        text_engine_port=5910,
        route_dir=tmp_path / "commands",
        text_engine_log_path=tmp_path / "text.log",
        model_state_path=tmp_path / "runtime.json",
    )
    assert settings.image_engine_port == 5911
    assert settings.image_command_dir == tmp_path / "image-commands"
    assert settings.image_engine_log_path == tmp_path / "image-worker.log"
    assert settings.image_state_path == tmp_path / "image-runtime.json"
    assert settings.image_result_dir == tmp_path / "image-results"


def test_openai_parameters_translate_without_losing_experimental_controls() -> None:
    translated = translate_openai_request(
        {
            "prompt": "red teapot",
            "negative_prompt": "letters",
            "size": "768x512",
            "steps": 4,
            "guidance_scale": 3.5,
            "sampler_name": "euler",
            "scheduler": "simple",
        },
        42,
    )
    assert translated == {
        "prompt": "red teapot",
        "negative_prompt": "letters",
        "width": 768,
        "height": 512,
        "steps": 4,
        "cfg_scale": 3.5,
        "seed": 42,
        "sampler_name": "euler",
        "scheduler": "simple",
    }


def test_profile_defaults_supply_settings_missing_from_simple_frontends() -> None:
    translated = translate_openai_request(
        {"prompt": "documentary photograph", "size": "512x512"},
        7,
        default_steps=4,
        default_cfg_scale=1.0,
        default_sampler="euler",
        default_scheduler="",
    )
    assert translated["steps"] == 4
    assert translated["cfg_scale"] == 1.0
    assert translated["sampler_name"] == "euler"
    assert "scheduler" not in translated


def test_frontend_values_override_profile_generation_defaults() -> None:
    translated = translate_openai_request(
        {
            "prompt": "documentary photograph",
            "steps": 8,
            "cfg_scale": 2.5,
            "sampler_name": "heun",
            "scheduler": "simple",
        },
        7,
        default_steps=4,
        default_cfg_scale=1.0,
        default_sampler="euler",
    )
    assert translated["steps"] == 8
    assert translated["cfg_scale"] == 2.5
    assert translated["sampler_name"] == "heun"
    assert translated["scheduler"] == "simple"


def test_image_dimensions_must_be_multiples_of_64() -> None:
    with pytest.raises(ImageEngineError, match="multiple of 64"):
        translate_openai_request({"prompt": "x", "size": "1000x1000"}, 1)


@pytest.mark.asyncio
async def test_image_generation_increments_fixed_seeds_and_returns_openai_data(
    tmp_path: Path,
) -> None:
    store, profile = image_profile(tmp_path)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    settings = Settings(
        image_command_dir=tmp_path / "image-commands",
        image_engine_port=port,
        image_engine_log_path=tmp_path / "image.log",
        image_state_path=tmp_path / "image-state.json",
        image_result_dir=tmp_path / "results",
    )
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        seed = body["seed"]
        return httpx.Response(
            200,
            json={"images": [f"encoded-{seed}"], "info": json.dumps({"seed": seed})},
        )

    control = ImageControlPlane(settings, store)
    await control.client.aclose()
    control.client = httpx.AsyncClient(
        base_url="http://image.test", transport=httpx.MockTransport(handler)
    )
    control.ensure_model = AsyncMock(return_value=True)  # type: ignore[method-assign]

    result = await control.generate(
        {"model": profile.id, "prompt": "red teapot", "n": 3, "seed": 900}
    )

    assert [request["seed"] for request in seen] == [900, 901, 902]
    assert all(request["steps"] == 4 for request in seen)
    assert all(request["cfg_scale"] == 1.0 for request in seen)
    assert all(request["sampler_name"] == "euler" for request in seen)
    assert [item["b64_json"] for item in result] == [
        "encoded-900",
        "encoded-901",
        "encoded-902",
    ]
    assert all(item["model"] == profile.id for item in result)
    assert control.generation_lock.locked() is False
    await control.client.aclose()
