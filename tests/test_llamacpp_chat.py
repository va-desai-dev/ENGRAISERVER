from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from engrai_server.domain.store import DeploymentStore
from engrai_server.engines.llamacpp import chat
from engrai_server.llamacpp import LlamaCppControlPlane
from engrai_server.profiles import ProfileStore
from engrai_server.settings import Settings


GEMMA4 = (Path(__file__).parent / "fixtures" / "gemma4-style-template.jinja").read_text(
    encoding="utf-8"
)
THINKING = {"enable_thinking": True}


def roleplay_messages(*, prefill: str | None = "<think>\n</think>") -> list[dict[str, str]]:
    """Character card -> history -> post-history instruction -> prefill."""

    messages = [
        {"role": "system", "content": "You are Mira, a sardonic ship's navigator."},
        {"role": "assistant", "content": "*Mira glances up from the charts.* Lost again?"},
        {"role": "user", "content": "Where are we?"},
        {
            "role": "assistant",
            "content": "<|channel>thought\nplan the reply<channel|>Somewhere south of trouble.",
        },
        {"role": "user", "content": "Can you get us home?"},
        {"role": "system", "content": "[Stay in character. Reply in two paragraphs.]"},
    ]
    if prefill is not None:
        messages.append({"role": "assistant", "content": prefill})
    return messages


def test_roleplay_prefill_is_appended_after_the_native_generation_prompt() -> None:
    prompt = chat.render_prompt(GEMMA4, roleplay_messages(), THINKING)

    assert prompt.startswith("<|turn>system\n<|think|>\nYou are Mira")
    assert "<|turn>system\n[Stay in character. Reply in two paragraphs.]<turn|>\n" in prompt
    # Thinking stays on: no closed empty thought channel is injected.
    assert prompt.endswith("<|turn>model\n<think>\n</think>")
    assert "<|channel>thought\n<channel|>" not in prompt
    # The template strips prior thoughts from history on its own.
    assert "plan the reply" not in prompt
    assert "<|turn>model\nSomewhere south of trouble.<turn|>\n" in prompt


def test_rendered_history_is_a_stable_prefix_across_turns() -> None:
    history = roleplay_messages(prefill=None)[:-1]
    first = chat.render_prompt(GEMMA4, history, THINKING)
    following = chat.render_prompt(
        GEMMA4,
        history
        + [
            {"role": "assistant", "content": "<|channel>thought\nroute<channel|>Hold on."},
            {"role": "user", "content": "Faster."},
        ],
        THINKING,
    )

    assert following.startswith(first)


def test_template_exceptions_do_not_reject_the_request() -> None:
    strict = (
        "{% for m in messages %}"
        "{% if loop.index0 > 0 and m.role == messages[loop.index0 - 1].role %}"
        "{{ raise_exception('Conversation roles must alternate') }}{% endif %}"
        "<{{ m.role }}>{{ m.content }}"
        "{% endfor %}{% if add_generation_prompt %}<assistant>{% endif %}"
    )
    prompt = chat.render_prompt(
        strict,
        [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}],
    )

    assert prompt == "<user>a<user>b<assistant>"


def test_blank_trailing_assistant_is_not_a_prefill() -> None:
    prompt = chat.render_prompt(
        GEMMA4,
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": " \n"}],
        THINKING,
    )

    assert prompt.endswith("<|turn>model\n<turn|>\n<|turn>model\n")


def split(prompt: str, pieces: list[str]) -> tuple[str, str]:
    splitter = chat.ReasoningSplitter(prompt)
    parts = [part for piece in pieces for part in splitter.feed(piece)] + splitter.finish()
    reasoning = "".join(text for kind, text in parts if kind == "reasoning")
    content = "".join(text for kind, text in parts if kind == "content")
    return reasoning, content


def test_splitter_handles_markers_broken_across_stream_chunks() -> None:
    reasoning, content = split(
        "<|turn>model\n",
        ["<|chan", "nel>thought\nWe should", " hurry.<chan", "nel|>", "\n*She tugs the helm.*"],
    )

    assert reasoning == "We should hurry."
    assert content == "*She tugs the helm.*"


def test_splitter_keeps_prefill_continuations_verbatim() -> None:
    reasoning, content = split("<|turn>model\nMira:", [" Hold", " on."])

    assert reasoning == ""
    assert content == " Hold on."


def test_splitter_treats_output_as_reasoning_when_the_prompt_opened_thinking() -> None:
    reasoning, content = split("<|im_start|>assistant\n<think>\n", ["plan", "</think>", "Done"])

    assert (reasoning, content) == ("plan", "Done")


def test_completion_request_maps_openai_sampling() -> None:
    body = chat.completion_request(
        {
            "model": "gemma",
            "messages": [],
            "max_tokens": 300,
            "repetition_penalty": 1.1,
            "min_p": 0.05,
            "stop": "###",
            "stream": True,
            "user": "ignored",
            "response_format": {"type": "json_object"},
        },
        "PROMPT",
    )

    assert body == {
        "n_predict": 300,
        "repeat_penalty": 1.1,
        "min_p": 0.05,
        "stop": ["###"],
        "json_schema": {"type": "object"},
        "prompt": "PROMPT",
        "stream": True,
        "cache_prompt": True,
    }


def test_tool_and_image_requests_stay_on_the_native_handler() -> None:
    user = {"role": "user", "content": "hi"}
    assert chat.supports({"messages": [user]})
    assert not chat.supports({"messages": [user], "tools": [{"type": "function"}]})
    assert not chat.supports(
        {"messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}]}
    )


class OneChunkStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes):
        self.content = content

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self.content


def sse(*events: dict[str, object]) -> bytes:
    return b"".join(f"data: {json.dumps(event)}\n\n".encode() for event in events)


def control_plane(tmp_path: Path, handler) -> LlamaCppControlPlane:  # type: ignore[no-untyped-def]
    model = tmp_path / "gemma.gguf"
    model.write_bytes(b"GGUF")
    profiles = ProfileStore(tmp_path / "commands", [tmp_path])
    profiles.save(
        "gemma",
        name="Gemma",
        model_path=str(model),
        prompt={"template": "jinja", "adapter": "auto", "thinking": True},
    )
    settings = Settings(
        control_api_keys="api-key",
        control_admin_token="admin-key",
        engine_host="127.0.0.1",
        text_engine_port=51112,
        route_dir=tmp_path / "commands",
        model_search_roots=str(tmp_path),
        model_state_path=tmp_path / "state.json",
        text_engine_log_path=tmp_path / "worker.log",
    )
    control = LlamaCppControlPlane(settings, profiles, DeploymentStore(tmp_path / "deployments"))
    control.client = httpx.AsyncClient(
        base_url="http://worker.test", transport=httpx.MockTransport(handler)
    )
    return control


@pytest.mark.asyncio
async def test_chat_completions_stream_through_the_raw_completion_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"chat_template": GEMMA4})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=sse(
                {"content": "<|channel>thought\nkeep it short", "stop": False},
                {"content": "<channel|>Aye, captain.", "stop": False},
                {
                    "content": "",
                    "stop": True,
                    "stop_type": "eos",
                    "tokens_evaluated": 120,
                    "tokens_predicted": 9,
                    "timings": {"prompt_n": 14, "cache_n": 106},
                },
            ),
        )

    control = control_plane(tmp_path, handler)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))
    request = {
        "model": "gemma",
        "messages": roleplay_messages(),
        "stream": True,
        "stream_options": {"include_usage": True},
        "temperature": 0.8,
    }

    response, stream = await control.forward_openai(
        "POST", "chat/completions", json.dumps(request).encode(), "", "application/json"
    )
    events = [
        line[6:]
        for line in b"".join([chunk async for chunk in stream]).decode().split("\n\n")
        if line.startswith("data: ")
    ]

    path, body = seen[0]
    assert path == "/completion"
    assert body["cache_prompt"] is True and body["temperature"] == 0.8
    assert str(body["prompt"]).endswith("<|turn>model\n<think>\n</think>")
    assert response.headers["content-type"] == "text/event-stream"
    chunks = [json.loads(event) for event in events[:-1]]
    assert events[-1] == "[DONE]"
    deltas = [chunk["choices"][0]["delta"] for chunk in chunks if chunk["choices"]]
    assert {"reasoning_content": "keep it short"} in deltas
    assert {"content": "Aye, captain."} in deltas
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-2]["timings"]["prompt_n"] == 14
    assert chunks[-1]["usage"] == {
        "prompt_tokens": 120,
        "completion_tokens": 9,
        "total_tokens": 129,
    }
    assert not control.generation_lock.locked()
    await control.client.aclose()


@pytest.mark.asyncio
async def test_non_streaming_chat_returns_a_split_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"chat_template": GEMMA4})
        return httpx.Response(
            200,
            json={
                "content": "<|channel>thought\nhm<channel|>Fine.",
                "stop_type": "limit",
                "tokens_evaluated": 10,
                "tokens_predicted": 4,
            },
        )

    control = control_plane(tmp_path, handler)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))

    response, stream = await control.forward_openai(
        "POST",
        "chat/completions",
        json.dumps({"model": "gemma", "messages": [{"role": "user", "content": "hi"}]}).encode(),
        "",
        "application/json",
    )
    result = json.loads(b"".join([chunk async for chunk in stream]))

    assert response.headers["content-type"] == "application/json"
    assert result["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Fine.",
        "reasoning_content": "hm",
    }
    assert result["choices"][0]["finish_reason"] == "length"
    assert not control.generation_lock.locked()
    await control.client.aclose()


@pytest.mark.asyncio
async def test_tool_requests_still_proxy_to_native_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, stream=OneChunkStream(b'{"ok": true}'))

    control = control_plane(tmp_path, handler)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))
    request = {
        "model": "gemma",
        "messages": [{"role": "user", "content": "weather?"}],
        "tools": [{"type": "function", "function": {"name": "weather"}}],
    }

    _, stream = await control.forward_openai(
        "POST", "chat/completions", json.dumps(request).encode(), "", "application/json"
    )
    [chunk async for chunk in stream]

    assert paths == ["/v1/chat/completions"]
    await control.client.aclose()
