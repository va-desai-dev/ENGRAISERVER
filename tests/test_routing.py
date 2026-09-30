from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from engrai_server.domain.store import DeploymentStore
from engrai_server.llamacpp import LlamaCppControlPlane
from engrai_server.profiles import ProfileStore
from engrai_server.settings import Settings


class OneChunkStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):
        yield self.content


@pytest.mark.asyncio
async def test_gateway_consumes_route_id_but_preserves_openai_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_file = tmp_path / "alpha-Q4_K_M.gguf"
    model_file.write_bytes(b"gguf")
    profiles = ProfileStore(tmp_path / "commands", [])
    profile = profiles.save(
        "alpha",
        name="Alpha",
        model_path=str(model_file),
    )
    settings = Settings(
        control_api_keys="api-key",
        control_admin_token="admin-key",
                engine_host="127.0.0.1",
        text_engine_port=51111,
        route_dir=tmp_path / "commands",
        model_search_roots=str(tmp_path),
        model_state_path=tmp_path / "state.json",
        text_engine_log_path=tmp_path / "worker.log",
    )
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = json.dumps(body).encode()
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=OneChunkStream(content),
        )

    control = LlamaCppControlPlane(settings, profiles, DeploymentStore(tmp_path / "deployments"))
    await control.client.aclose()
    control.client = httpx.AsyncClient(
        base_url="http://worker.test",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=True))

    original = {
        "model": "alpha",
        "prompt": "hello",
        "stream": True,
        "temperature": 0.37,
    }
    response, stream = await control.forward_openai(
        "POST",
        "completions",
        json.dumps(original).encode(),
        "",
        "application/json",
    )
    payload = json.loads(b"".join([chunk async for chunk in stream]))

    assert response.status_code == 200
    assert payload == {**original, "model": profile.id}
    assert seen == [{**original, "model": profile.id}]
    control.ensure_model.assert_awaited_once_with(profile)
    assert control.generation_lock.locked() is False
    await control.client.aclose()
