from __future__ import annotations

import base64
import importlib
import json
import re
import shutil
import struct
import subprocess
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from engrai_server.cardgen.imaging import image_dimensions, snap_size
from engrai_server.cardgen.store import parse_image, to_v2

ROOT = Path(__file__).resolve().parents[1]


def png(width: int, height: int) -> str:
    """A real, minimal PNG as a data: URL."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    content = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(content).decode()


CHARACTER = {
    "name": "Maëlle",
    "description": "I restore clocks.",
    "firstMessage": 'I look up. "Come in."',
    "postHistoryInstructions": "I use dry wit.",
    "alternateGreetings": ["I wave."],
    "extensions": {"custom": {"keep": True}},
    "character_book": {"name": "Town", "entries": []},
}


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_API_KEYS", "studio-api-key")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(tmp_path / "routes"))
    monkeypatch.setenv("ENGRAI_IMAGE_COMMAND_DIR", str(tmp_path / "image-commands"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text.log"))
    monkeypatch.setenv("ENGRAI_IMAGE_ENGINE_LOG", str(tmp_path / "image.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("IMAGE_STATE_PATH", str(tmp_path / "image-runtime.json"))
    monkeypatch.setenv("IMAGE_RESULT_DIR", str(tmp_path / "results"))
    monkeypatch.setenv("CARDGEN_DATA_DIR", str(tmp_path / "cardgen"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5928")
    monkeypatch.setenv("ENGRAI_IMAGE_ENGINE_PORT", "5929")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    with TestClient(module.app) as client:
        token, _ = module.credentials.create_session()
        client.headers["Authorization"] = f"Bearer {token}"
        yield client, module, token
    get_settings.cache_clear()


def load_text(module, model: str = "writer") -> None:
    module.control.state.active_model = model
    module.control.state.phase = "ready"


def load_image(module, model: str = "painter") -> None:
    module.images.state.active_model = model
    module.images.state.phase = "ready"


def fake_engine(module, frames: list[bytes], *, content_type: str = "text/event-stream"):
    """Stand in for the text engine, recording what Card Studio asked it for."""
    seen: list[dict] = []

    async def forward(method, path, body, query, ctype, *, ledger=True):
        # Authoring prompts must never carry the owner's ledger.
        assert ledger is False
        seen.append({"method": method, "path": path, "body": json.loads(body)})

        async def stream() -> AsyncIterator[bytes]:
            for frame in frames:
                yield frame

        return httpx.Response(200, headers={"content-type": content_type}), stream()

    module.control.forward_openai = forward
    return seen


def sse_chunk(text: str) -> bytes:
    return f"data: {json.dumps({'choices': [{'index': 0, 'delta': {'content': text}}]})}\n\n".encode()


# ── Page and session ────────────────────────────────────────────────────


def test_studio_page_and_scripts_are_served_without_provider_or_login_code(studio) -> None:
    client, _, _ = studio
    client.headers.pop("Authorization")
    assert client.get("/cardgen", follow_redirects=False).headers["location"] == "/cardgen/"
    page = client.get("/cardgen/")
    assert page.status_code == 200
    assert "engrai-bridge.js" in page.text
    assert 'id="auth-overlay"' not in page.text
    assert 'id="text-api-key"' not in page.text
    assert "fonts.googleapis.com" not in page.text
    assert client.get("/cardgen/src/scripts/engrai-bridge.js").status_code == 200
    assert client.get("/cardgen/src/scripts/auth.js").status_code == 404


def test_api_needs_the_console_session_not_an_api_key(studio) -> None:
    client, _, token = studio
    client.headers.pop("Authorization")
    refused = client.get("/cardgen/api/engine")
    assert refused.status_code == 401
    assert refused.headers["X-Session-Expired"] == "1"
    assert client.get(
        "/cardgen/api/engine", headers={"Authorization": "Bearer studio-api-key"}
    ).status_code == 401
    # Never in a URL: query strings end up in access logs.
    assert client.get(f"/cardgen/api/engine?token={token}").status_code == 401


def test_image_cookie_is_scoped_http_only_and_read_only(studio) -> None:
    client, _, token = studio
    issued = client.post("/cardgen/api/session/image-cookie")
    assert issued.status_code == 204
    header = issued.headers["set-cookie"].lower()
    assert "httponly" in header and "samesite=strict" in header and "path=/cardgen/api/" in header

    client.headers.pop("Authorization")
    client.cookies.set("engrai_studio", token, path="/cardgen/api/")
    assert client.get("/cardgen/api/engine").status_code == 200
    # Reads only, and it cannot mint itself a successor.
    assert client.post("/cardgen/api/config", json={"x": 1}).status_code == 401
    assert client.post("/cardgen/api/session/image-cookie").status_code == 401


def test_engine_reports_only_models_the_console_has_loaded(studio) -> None:
    client, module, _ = studio
    status = client.get("/cardgen/api/engine").json()
    assert status["text"]["model"] is None and status["image"]["model"] is None

    load_text(module)
    module.images.state.target_model = "painter"
    module.images.state.phase = "switching"
    status = client.get("/cardgen/api/engine").json()
    assert status["text"]["model"] == {"id": "writer", "name": "writer"}
    assert status["image"]["model"] is None
    assert status["image"]["loading"]["id"] == "painter"


# ── Text ────────────────────────────────────────────────────────────────


def test_text_without_a_loaded_model_is_refused_without_loading_one(studio) -> None:
    client, module, _ = studio
    module.control.forward_openai = AsyncMock()
    response = client.post(
        "/cardgen/api/text/chat/completions",
        json={"model": "anything", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_text_model"
    module.control.forward_openai.assert_not_called()


def test_text_always_targets_the_loaded_model_with_cardgen_defaults(studio) -> None:
    client, module, _ = studio
    load_text(module)
    reply = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
    seen = fake_engine(module, [json.dumps(reply).encode()], content_type="application/json")
    response = client.post(
        "/cardgen/api/text/chat/completions",
        headers={"X-API-Key": "sk-provider", "X-API-URL": "https://elsewhere.test"},
        json={
            "model": "gpt-provider-model",
            "messages": [{"role": "user", "content": "hi"}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "clientRef": "ignored-without-resumable",
        },
    )
    assert response.status_code == 200
    assert response.json() == reply
    body = seen[0]["body"]
    assert seen[0]["path"] == "chat/completions"
    assert body["model"] == "writer"
    assert body["temperature"] == 0  # zero survives; it is not "unset"
    assert body["max_tokens"] == 1000 and body["top_p"] == 0.95
    assert body["response_format"] == {"type": "json_object"}
    assert "clientRef" not in body


def test_resumable_stream_announces_its_job_and_can_be_replayed(studio) -> None:
    client, module, _ = studio
    load_text(module)
    frames = [
        sse_chunk("Maë"),
        sse_chunk("lle"),
        b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n',
        b"data: [DONE]\n\n",
    ]
    fake_engine(module, frames)
    live = client.post(
        "/cardgen/api/text/chat/completions",
        json={"messages": [{"role": "user", "content": "name?"}], "stream": True,
              "resumable": True, "clientRef": "ref-1"},
    )
    assert live.status_code == 200
    first, *rest = [line for line in live.text.split("\n\n") if line]
    job_id = json.loads(first.removeprefix("data: "))["jobId"]
    # The browser that started the job gets the engine's own frames verbatim.
    assert "\n\n".join(rest) + "\n\n" == b"".join(frames).decode()

    listed = client.get("/cardgen/api/jobs", params={"clientRef": "ref-1"}).json()["jobs"]
    assert [job["id"] for job in listed] == [job_id]
    assert listed[0]["status"] == "done" and listed[0]["length"] == len("Maëlle")

    replay = client.get(f"/cardgen/api/jobs/{job_id}/stream", params={"from": 3}).text
    assert json.loads(replay.split("\n\n")[0].removeprefix("data: "))["choices"][0]["delta"]["content"] == "lle"
    assert replay.rstrip().endswith("data: [DONE]")

    result = client.get(f"/cardgen/api/jobs/{job_id}/result").json()
    assert result["status"] == "done"
    assert result["result"]["choices"][0]["message"]["content"] == "Maëlle"


def test_resumable_text_reports_engine_failure_to_the_job(studio) -> None:
    client, module, _ = studio
    load_text(module)

    async def broken(*_args, **_options):
        raise httpx.ConnectError("worker gone")

    module.control.forward_openai = broken
    response = client.post(
        "/cardgen/api/text/chat/completions",
        json={"messages": [{"role": "user", "content": "x"}], "stream": True,
              "resumable": True, "clientRef": "ref-2"},
    )
    assert response.status_code == 502
    job = client.get("/cardgen/api/jobs", params={"clientRef": "ref-2"}).json()["jobs"][0]
    assert client.get(f"/cardgen/api/jobs/{job['id']}/result").json()["status"] == "error"


# ── Images ──────────────────────────────────────────────────────────────


def test_images_use_the_loaded_model_and_come_back_as_data_urls(studio) -> None:
    client, module, _ = studio
    load_image(module)
    module.images.generate = AsyncMock(
        return_value=[{"b64_json": "aW1hZ2U=", "seed": 9, "model": "painter"}]
    )
    response = client.post(
        "/cardgen/api/image/generations",
        json={"model": "provider-flux", "prompt": "a clockmaker", "width": 832, "height": 1216,
              "resumable": True, "clientRef": "img-1"},
    )
    assert response.status_code == 200
    item = response.json()["data"][0]
    assert item["url"] == "data:image/png;base64,aW1hZ2U="
    payload = module.images.generate.await_args.args[0]
    assert payload["model"] == "painter"
    assert payload["size"] == "832x1216"
    assert module.images.generate.await_args.kwargs == {
        "init_image": None, "references": (), "strength": None,
    }
    job = client.get("/cardgen/api/jobs", params={"clientRef": "img-1"}).json()["jobs"][0]
    assert client.get(f"/cardgen/api/jobs/{job['id']}/result").json()["result"]["data"][0]["seed"] == 9


def test_edits_are_img2img_at_the_source_shape_and_combine_needs_two_references(studio) -> None:
    client, module, _ = studio
    load_image(module)
    module.images.generate = AsyncMock(return_value=[{"b64_json": "eA==", "seed": 1, "model": "painter"}])
    source = png(600, 900)
    response = client.post(
        "/cardgen/api/image/generations",
        json={"prompt": "give her a red scarf", "image": source},
    )
    assert response.status_code == 200
    call = module.images.generate.await_args
    assert call.args[0]["size"] == "576x896"  # 600x900 on the 64px grid
    assert call.kwargs["init_image"] == source.split(",", 1)[1]
    assert call.kwargs["references"] == (source.split(",", 1)[1],)
    assert call.kwargs["strength"] == 0.8

    assert client.post(
        "/cardgen/api/image/combine", json={"prompt": "together", "input_references": [source]}
    ).status_code == 400
    assert client.post(
        "/cardgen/api/image/combine", json={"prompt": "together", "input_references": [source, png(64, 64)]}
    ).status_code == 200
    assert len(module.images.generate.await_args.kwargs["references"]) == 2

    unsafe = client.post(
        "/cardgen/api/image/generations",
        json={"prompt": "x", "image": "data:text/html;base64,PGgxPg=="},
    )
    assert unsafe.status_code == 400


def test_images_without_a_loaded_model_are_refused(studio) -> None:
    client, module, _ = studio
    module.images.generate = AsyncMock()
    response = client.post("/cardgen/api/image/generations", json={"prompt": "x"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_image_model"
    module.images.generate.assert_not_called()


async def test_image_engine_sends_init_and_reference_images_to_img2img(tmp_path: Path) -> None:
    from engrai_server.image_engine import ImageControlPlane
    from engrai_server.image_profiles import ImageProfileStore
    from engrai_server.settings import Settings

    for name in ("flux.gguf", "qwen.gguf", "vae.safetensors"):
        (tmp_path / name).write_bytes(b"weights")
    store = ImageProfileStore(tmp_path / "image-commands", [tmp_path])
    profile = store.save(
        "flux-test", name="FLUX Test", model_path=str(tmp_path / "flux.gguf"),
        text_encoder_path=str(tmp_path / "qwen.gguf"), vae_path=str(tmp_path / "vae.safetensors"),
    )
    settings = Settings(
        image_command_dir=tmp_path / "image-commands",
        image_engine_port=5930,
        image_engine_log_path=tmp_path / "image.log",
        image_state_path=tmp_path / "image-state.json",
        image_result_dir=tmp_path / "results",
    )
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"images": ["out"], "info": "{}"})

    control = ImageControlPlane(settings, store)
    await control.client.aclose()
    control.client = httpx.AsyncClient(base_url="http://image.test", transport=httpx.MockTransport(handler))
    control.ensure_model = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await control.generate(
        {"model": profile.id, "prompt": "scarf", "size": "512x768"},
        init_image="c3Jj", references=("c3Jj",), strength=0.6,
    )
    await control.generate({"model": profile.id, "prompt": "pair"}, references=("YQ==", "Yg=="))
    await control.client.aclose()

    (edit_path, edit), (combine_path, combine) = seen
    assert edit_path == "/sdapi/v1/img2img"
    assert edit["init_images"] == ["c3Jj"] and edit["extra_images"] == ["c3Jj"]
    assert edit["denoising_strength"] == 0.6
    assert combine_path == "/sdapi/v1/txt2img"
    assert combine["extra_images"] == ["YQ==", "Yg=="] and "init_images" not in combine


# ── Library ─────────────────────────────────────────────────────────────


def test_cards_save_as_v2_list_without_images_and_serve_thumbnails(studio, tmp_path: Path) -> None:
    client, _, token = studio
    portrait = png(4, 4)
    assert client.post("/cardgen/api/storage/cards", json={"character": {"name": "  "}}).status_code == 400

    saved = client.post(
        "/cardgen/api/storage/cards", json={"character": CHARACTER, "imageBase64": portrait}
    ).json()
    assert saved["card"]["spec"] == "chara_card_v2"
    assert saved["characterName"] == "Maëlle" and saved["isPermanent"] is True

    listed = client.get("/cardgen/api/storage/cards").json()
    assert [row["id"] for row in listed] == [saved["id"]]
    assert "imageBase64" not in listed[0]

    client.headers.pop("Authorization")
    client.cookies.set("engrai_studio", token, path="/cardgen/api/")
    thumb = client.get("/cardgen/api/storage/cards/thumbnail", params={"cardId": saved["id"]})
    assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/png"
    assert thumb.headers["x-content-type-options"] == "nosniff"
    client.cookies.clear()
    client.headers["Authorization"] = f"Bearer {token}"

    # Persisted beside the gateway's other data, as CardGen's file layout.
    stored = json.loads((tmp_path / "cardgen" / "cards.json").read_text(encoding="utf-8"))
    assert stored[0]["card"]["data"]["name"] == "Maëlle"

    assert client.delete(f"/cardgen/api/storage/cards/{saved['id']}").json() == {"success": True}
    assert client.get("/cardgen/api/storage/cards").json() == []


def test_unsafe_images_are_never_stored(studio) -> None:
    client, _, _ = studio
    html = "data:text/html;base64,PHNjcmlwdD4="
    assert client.post("/cardgen/api/storage/cards", json={"character": CHARACTER, "imageBase64": html}).status_code == 400
    assert client.post("/cardgen/api/storage/playground-images", json={"imageBase64": html}).status_code == 400


def test_gallery_add_reorder_and_delete(studio) -> None:
    client, _, _ = studio
    card = client.post("/cardgen/api/storage/cards", json={"character": CHARACTER}).json()
    base = f"/cardgen/api/storage/cards/{card['id']}/gallery"
    assert client.post("/cardgen/api/storage/cards/missing/gallery", json={"imageBase64": png(2, 2)}).status_code == 404
    first = client.post(base, json={"imageBase64": png(2, 2)}).json()
    second = client.post(base, json={"imageBase64": png(3, 3)}).json()
    assert client.get(first["url"]).status_code == 200
    assert client.put(f"{base}/reorder", json={"orderedIds": [first["id"]]}).status_code == 400
    assert client.put(f"{base}/reorder", json={"orderedIds": [second["id"], first["id"]]}).status_code == 200
    assert [row["id"] for row in client.get(base).json()] == [second["id"], first["id"]]
    client.delete(f"{base}/{first['id']}")
    assert [row["id"] for row in client.get(base).json()] == [second["id"]]


def test_config_and_prompts_round_trip(studio) -> None:
    client, _, _ = studio
    assert client.get("/cardgen/api/config").json() == {}
    client.post("/cardgen/api/config", json={"app": {"creator": "V"}})
    assert client.get("/cardgen/api/config").json() == {"app": {"creator": "V"}}
    prompt = client.post("/cardgen/api/storage/prompts", json={"concept": "a clockmaker"}).json()
    assert client.get(f"/cardgen/api/storage/prompts/{prompt['id']}").json()["concept"] == "a clockmaker"
    assert client.get("/cardgen/api/storage/elsewhere").status_code == 404


# ── Pure helpers ────────────────────────────────────────────────────────


def test_v2_serialization_matches_card_v2_js() -> None:
    card = to_v2(CHARACTER)
    assert card["spec"] == "chara_card_v2" and card["spec_version"] == "2.0"
    assert card["data"]["first_mes"] == CHARACTER["firstMessage"]
    assert card["data"]["post_history_instructions"] == "I use dry wit."
    assert card["data"]["character_book"] == {"name": "Town", "entries": []}
    assert to_v2(card) == card  # an exported card re-imports unchanged
    card["data"]["extensions"]["custom"]["keep"] = False
    assert CHARACTER["extensions"]["custom"]["keep"] is True


def test_image_helpers() -> None:
    assert image_dimensions(base64.b64decode(png(37, 19).split(",", 1)[1])) == (37, 19)
    assert snap_size(4000, 2000) == (2048, 1024)
    assert snap_size(10, 10) == (64, 64)
    assert parse_image("data:image/svg+xml;base64,PHN2Zz4=") is None
    assert parse_image(png(1, 1)).mime == "image/png"


def test_studio_theme_carries_the_console_brand_tokens_verbatim() -> None:
    def brand(text: str) -> dict[str, str]:
        return dict(re.findall(r"(--[a-z-]+):\s*([^;]+);", text))

    console = (ROOT / "ui/src/styles/tokens.css").read_text(encoding="utf-8")
    console_brand = console.split("/* Brand", 1)[1].split("/* Text roles", 1)[0]
    studio = (ROOT / "src/engrai_server/cardgen/web/src/styles/engrai-theme.css").read_text(encoding="utf-8")
    studio_brand = studio.split("engrai-brand:start */", 1)[1].split("/* engrai-brand:end", 1)[0]
    assert brand(studio_brand) == brand(console_brand)


def test_studio_scripts_reach_no_outside_host() -> None:
    """Card Studio talks only to its own gateway: no CDNs, providers or scrapers."""
    web = ROOT / "src/engrai_server/cardgen/web"
    remote_fetch = re.compile(r"fetch\(\s*[\"'`]https?://")
    remote_asset = re.compile(r"<(?:script|link)\b[^>]*(?:src|href)=\"https?://")
    remote_css = re.compile(r"@import\s+url\(\s*[\"']?https?://|url\(\s*[\"']?https?://")
    offenders = []
    for path in [web / "index.html", *web.rglob("*.js"), *web.rglob("*.css")]:
        text = path.read_text(encoding="utf-8")
        for pattern in (remote_fetch, remote_asset, remote_css):
            offenders += [f"{path.relative_to(web)}: {m.group(0)}" for m in pattern.finditer(text)]
    assert not offenders, offenders
    # The removed provider routes stay removed.
    scripts = "\n".join(p.read_text(encoding="utf-8") for p in web.rglob("*.js"))
    for route in ("/api/st/", "/api/search/", "/api/import/url", "/api/auth/"):
        assert route not in scripts, route


# ── World lorebooks ─────────────────────────────────────────────────────


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run the browser module")
def test_lorebook_topic_generation_and_draft_recovery() -> None:
    result = subprocess.run(
        ["node", str(ROOT / "tests/cardgen_topics.test.cjs")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_topic_schema_and_thinking_setting_reach_the_loaded_engine(studio) -> None:
    client, module, _ = studio
    load_text(module)
    reply = {"choices": [{"message": {"content": '{"topics":["Saltmarket"]}'}, "finish_reason": "stop"}]}
    seen = fake_engine(module, [json.dumps(reply).encode()], content_type="application/json")
    schema = {"type": "json_object", "schema": {"type": "object", "properties": {
        "topics": {"type": "array", "maxItems": 5, "items": {"type": "string", "maxLength": 120}},
    }, "required": ["topics"], "additionalProperties": False}}
    response = client.post("/cardgen/api/text/chat/completions", json={
        "messages": [{"role": "user", "content": "Suggest topics"}],
        "max_tokens": 1536, "response_format": schema,
        "chat_template_kwargs": {"enable_thinking": False}, "resumable": True,
    })
    assert response.status_code == 200 and response.json() == reply
    assert seen[0]["body"]["response_format"] == schema
    assert seen[0]["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen[0]["body"]["max_tokens"] == 1536


def test_world_lorebooks_save_validate_and_round_trip(studio) -> None:
    client, _, _ = studio
    base = "/cardgen/api/storage/lorebooks"
    entry = {"id": "e1", "title": "Saltmarket", "keys": ["Saltmarket"], "secondaryKeys": [],
             "content": "A fish market.", "constant": False, "enabled": True}
    assert client.post(base, json={"name": " ", "entries": [entry]}).status_code == 400
    assert client.post(base, json={"name": "Coast", "entries": [{"keys": "Saltmarket"}]}).status_code == 400
    saved = client.post(base, json={"name": "The Drowned Coast", "setting": "Tidal towns.", "entries": [entry]}).json()
    assert client.get(f"{base}/{saved['id']}").json()["entries"] == [entry]
    assert [row["name"] for row in client.get(base).json()] == ["The Drowned Coast"]
    # Saving again with the id updates in place rather than duplicating.
    client.post(base, json={"id": saved["id"], "name": "The Drowned Coast", "entries": [entry, {**entry, "id": "e2"}]})
    rows = client.get(base).json()
    assert len(rows) == 1 and len(rows[0]["entries"]) == 2
    client.delete(f"{base}/{saved['id']}")
    assert client.get(base).json() == []


@pytest.mark.skipif(shutil.which("node") is None, reason="needs Node.js to run the browser module")
def test_world_lorebook_formats_round_trip_through_sillytavern_world_info() -> None:
    script = ROOT / "src/engrai_server/cardgen/web/src/scripts/world-lorebook.js"
    harness = f"""
      const vm = require('node:vm'), fs = require('node:fs');
      const ctx = {{ window: {{}}, console, Date, Math, JSON }};
      ctx.APIHandler = function () {{}}; ctx.CharacterGeneratorApp = function () {{}};
      ctx.CharacterGeneratorApp.prototype.init = async function () {{}};
      vm.createContext(ctx);
      vm.runInContext(fs.readFileSync({json.dumps(str(script))}, 'utf8'), ctx);
      const {{ toWorldInfo, fromAnyLorebook }} = ctx.window.WorldLorebook;
      const book = {{ name: 'Coast', setting: 'Tides', entries: [
        {{ id: 'a', title: '', keys: ['Saltmarket', 'fish market'], secondaryKeys: ['night'], content: 'Market.', constant: true, enabled: true }},
        {{ id: 'b', title: 'Bells', keys: ['tide bells'], secondaryKeys: [], content: 'Bells.', constant: false, enabled: false }} ] }};
      const wi = toWorldInfo(book);
      const back = fromAnyLorebook(JSON.parse(JSON.stringify(wi)), 'x');
      const card = fromAnyLorebook({{ spec: 'chara_card_v2', data: {{ name: 'Ash', character_book: {{ name: 'Ash Lorebook',
        entries: [{{ keys: ['lighthouse'], content: 'Old.', secondary_keys: [], enabled: true }}] }} }} }}, 'x');
      let refused = '';
      try {{ fromAnyLorebook({{ spec: 'chara_card_v2', data: {{ name: 'Empty' }} }}, 'x'); }} catch (e) {{ refused = e.message; }}
      console.log(JSON.stringify({{ wi, back, card, refused }}));
    """
    result = json.loads(subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True).stdout)
    first = result["wi"]["entries"]["0"]
    assert first["key"] == ["Saltmarket", "fish market"] and first["keysecondary"] == ["night"]
    assert first["comment"] == "Saltmarket"  # untitled entries are named by their first key
    assert first["constant"] is True and first["selective"] is True
    assert result["wi"]["entries"]["1"]["disable"] is True
    assert [e["keys"] for e in result["back"]["entries"]] == [["Saltmarket", "fish market"], ["tide bells"]]
    assert result["back"]["entries"][1]["enabled"] is False and result["back"]["setting"] == "Tides"
    assert result["card"]["name"] == "Ash" and result["card"]["entries"][0]["keys"] == ["lighthouse"]
    assert result["refused"] == "That card has no lorebook."
