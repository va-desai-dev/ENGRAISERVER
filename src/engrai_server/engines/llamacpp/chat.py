"""OpenAI chat completions rendered by the gateway, generated as raw text.

llama-server's own chat handler rewrites trailing assistant prefills (it
injects a closed native think block and builds its reasoning parser from the
pre-rewrite prompt) and enforces strict template roles. Roleplay clients rely
on prefills, post-history system turns, and consecutive same-role messages, so
the gateway renders the prompt itself:

1. render ``messages`` with the model's own GGUF template, leniently;
2. append a trailing assistant prefill verbatim after the generation prompt;
3. generate from the flat prompt through ``/completion`` with prompt caching;
4. split reasoning from content with a fixed table of think markers.

A deterministic render keeps the token prefix identical between turns, so the
worker's prompt cache only processes the newest turn.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from datetime import datetime
from typing import Any

from jinja2.ext import Extension, loopcontrols
from jinja2.sandbox import ImmutableSandboxedEnvironment


# (start, end) think-marker pairs across common chat template families.
THINK_FORMATS: tuple[tuple[str, str], ...] = (
    ("<|channel|>analysis<|message|>", "<|start|>assistant<|channel|>final<|message|>"),
    (" to=self<|message|>", "<|eom|><|start|>assistant"),
    ("<think>", "</think>"),
    ("<seed:think>", "</seed:think>"),
    ("<|START_THINKING|>", "<|END_THINKING|>"),
    ("<|channel>thought", "<channel|>"),
    ("[THINK]", "[/THINK]"),
)

# /completion options forwarded verbatim when a client sends them.
_SAMPLING_KEYS = frozenset(
    {
        "temperature",
        "dynatemp_range",
        "dynatemp_exponent",
        "top_k",
        "top_p",
        "min_p",
        "typical_p",
        "top_n_sigma",
        "min_keep",
        "repeat_penalty",
        "repeat_last_n",
        "presence_penalty",
        "frequency_penalty",
        "dry_multiplier",
        "dry_base",
        "dry_allowed_length",
        "dry_penalty_last_n",
        "dry_sequence_breakers",
        "xtc_probability",
        "xtc_threshold",
        "mirostat",
        "mirostat_tau",
        "mirostat_eta",
        "samplers",
        "seed",
        "logit_bias",
        "ignore_eos",
        "n_keep",
        "grammar",
        "json_schema",
        "t_max_predict_ms",
    }
)
_ALIASES = {
    "max_tokens": "n_predict",
    "max_completion_tokens": "n_predict",
    "n_predict": "n_predict",
    "repetition_penalty": "repeat_penalty",
}


class _IgnoreGenerationTags(Extension):
    """Accept HF ``{% generation %}`` blocks by rendering their body."""

    tags = {"generation"}

    def parse(self, parser):  # type: ignore[no-untyped-def]
        parser.stream.skip(1)
        return parser.parse_statements(("name:endgeneration",), drop_needle=True)


def _tojson(
    value: Any,
    ensure_ascii: bool = False,
    indent: int | None = None,
    separators: tuple[str, str] | None = None,
    sort_keys: bool = False,
) -> str:
    return json.dumps(
        value, ensure_ascii=ensure_ascii, indent=indent, separators=separators, sort_keys=sort_keys
    )


def _environment() -> ImmutableSandboxedEnvironment:
    env = ImmutableSandboxedEnvironment(
        trim_blocks=True,
        lstrip_blocks=True,
        extensions=[_IgnoreGenerationTags, loopcontrols],
    )
    env.globals["strftime_now"] = lambda fmt="%Y-%m-%d %H:%M:%S": datetime.now().strftime(fmt)
    # Templates that "raise" on unusual role orders render the rest instead.
    env.globals["raise_exception"] = lambda message="": ""
    env.filters["tojson"] = _tojson
    return env


_ENV = _environment()


def _text_of(content: Any) -> str | None:
    """Return the plain text of string or text-part content, else None."""

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if not isinstance(item, Mapping) or item.get("type") != "text":
                return None
            parts.append(str(item.get("text") or ""))
        return "".join(parts)
    return None


def supports(payload: Mapping[str, Any]) -> bool:
    """Whether the gateway renderer covers this request.

    Tool calling, multimodal parts, and multi-choice sampling stay on
    llama-server's native handler, which parses them.
    """

    if payload.get("tools") or payload.get("functions"):
        return False
    if payload.get("n") not in (None, 1) or payload.get("logprobs"):
        return False
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    return all(
        isinstance(message, Mapping)
        and isinstance(message.get("role"), str)
        and _text_of(message.get("content")) is not None
        for message in messages
    )


def render_prompt(
    template: str,
    messages: Iterable[Mapping[str, Any]],
    template_kwargs: Mapping[str, Any] | None = None,
) -> str:
    """Render chat messages to the raw prompt the model sees.

    BOS/EOS render empty because llama-server adds BOS when tokenizing. A
    trailing, non-blank assistant message is a prefill: it is removed from the
    render and appended verbatim after the generation prompt.
    """

    normalized: list[dict[str, Any]] = []
    for message in messages:
        copied = json.loads(json.dumps(dict(message)))
        copied["content"] = _text_of(copied.get("content")) or ""
        normalized.append(copied)

    prefill = ""
    if (
        len(normalized) > 1
        and normalized[-1]["role"].lower() == "assistant"
        and normalized[-1]["content"].strip()
    ):
        prefill = normalized.pop()["content"]

    kwargs = dict(template_kwargs or {})
    kwargs.update(add_generation_prompt=True, bos_token="", eos_token="")
    prompt = _ENV.from_string(template).render(messages=normalized, **kwargs)
    return prompt + prefill


def template_kwargs(
    payload: Mapping[str, Any],
    *,
    thinking: bool,
    reasoning_effort: str | None,
) -> dict[str, Any]:
    """Merge deployment defaults with a client's ``chat_template_kwargs``."""

    kwargs: dict[str, Any] = {}
    if thinking:
        kwargs["enable_thinking"] = True
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    requested = payload.get("chat_template_kwargs")
    if isinstance(requested, Mapping):
        kwargs.update(requested)
    if payload.get("reasoning_effort"):
        kwargs["reasoning_effort"] = payload["reasoning_effort"]
    if "enable_thinking" in kwargs and not kwargs["enable_thinking"]:
        kwargs["reasoning_strength"] = "none"
    elif kwargs.get("reasoning_effort"):
        kwargs["reasoning_strength"] = kwargs["reasoning_effort"]
    return kwargs


def completion_request(payload: Mapping[str, Any], prompt: str) -> dict[str, Any]:
    """Translate an OpenAI chat request into a llama-server /completion body."""

    body: dict[str, Any] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if key in _SAMPLING_KEYS:
            body[key] = value
        elif key in _ALIASES:
            body[_ALIASES[key]] = value
    stop = payload.get("stop")
    if isinstance(stop, str):
        body["stop"] = [stop]
    elif isinstance(stop, list):
        body["stop"] = [item for item in stop if isinstance(item, str) and item]
    response_format = payload.get("response_format")
    if isinstance(response_format, Mapping):
        if response_format.get("type") == "json_schema":
            schema = (response_format.get("json_schema") or {}).get("schema")
            if isinstance(schema, Mapping):
                body["json_schema"] = schema
        elif response_format.get("type") == "json_object":
            body["json_schema"] = {"type": "object"}
    body.update(prompt=prompt, stream=bool(payload.get("stream")), cache_prompt=True)
    return body


class ReasoningSplitter:
    """Incrementally route generated text into reasoning or content.

    Output is reasoning when the prompt already opened a think block, or when
    the first non-whitespace output is a known think marker. Everything after
    the matching end marker is content.
    """

    def __init__(self, prompt: str) -> None:
        self.state = "detect"
        self.end = ""
        self.buffer = ""
        self.trim = False
        tail = prompt.rstrip()
        for start, end in THINK_FORMATS:
            if tail.endswith(start):
                self._enter("reasoning", end)
                break

    def _enter(self, state: str, end: str = "") -> None:
        self.state = state
        self.end = end
        self.trim = True

    def _emit(self, out: list[tuple[str, str]], kind: str, text: str) -> None:
        if self.trim:
            text = text.lstrip()
            if not text:
                return
            self.trim = False
        if text:
            out.append((kind, text))

    def feed(self, text: str) -> list[tuple[str, str]]:
        self.buffer += text
        out: list[tuple[str, str]] = []
        while True:
            if self.state == "detect":
                stripped = self.buffer.lstrip()
                if not stripped:
                    return out
                match = next(
                    ((start, end) for start, end in THINK_FORMATS if stripped.startswith(start)),
                    None,
                )
                if match:
                    self.buffer = stripped[len(match[0]) :]
                    self._enter("reasoning", match[1])
                    continue
                if any(start.startswith(stripped) for start, _ in THINK_FORMATS):
                    return out
                # Prefill continuations depend on leading spaces; keep them.
                self.state = "content"
                continue
            if self.state == "reasoning":
                index = self.buffer.find(self.end)
                if index >= 0:
                    self._emit(out, "reasoning", self.buffer[:index])
                    self.buffer = self.buffer[index + len(self.end) :]
                    self._enter("content")
                    continue
                hold = 0
                for size in range(min(len(self.end) - 1, len(self.buffer)), 0, -1):
                    if self.end.startswith(self.buffer[-size:]):
                        hold = size
                        break
                self._emit(out, "reasoning", self.buffer[: len(self.buffer) - hold])
                self.buffer = self.buffer[len(self.buffer) - hold :]
                return out
            self._emit(out, "content", self.buffer)
            self.buffer = ""
            return out

    def finish(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        kind = "reasoning" if self.state == "reasoning" else "content"
        self._emit(out, kind, self.buffer)
        self.buffer = ""
        return out


def _finish_reason(result: Mapping[str, Any]) -> str:
    return "length" if result.get("stop_type") == "limit" else "stop"


def _usage(result: Mapping[str, Any]) -> dict[str, int]:
    prompt_tokens = int(result.get("tokens_evaluated") or 0)
    completion_tokens = int(result.get("tokens_predicted") or 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def chat_completion(result: Mapping[str, Any], prompt: str, model: str) -> dict[str, Any]:
    """Build a non-streaming OpenAI response from a /completion result."""

    splitter = ReasoningSplitter(prompt)
    parts = splitter.feed(str(result.get("content") or "")) + splitter.finish()
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text for kind, text in parts if kind == "content"),
    }
    reasoning = "".join(text for kind, text in parts if kind == "reasoning")
    if reasoning:
        message["reasoning_content"] = reasoning
    response: dict[str, Any] = {
        "id": f"chatcmpl-{secrets.token_hex(12)}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": _finish_reason(result)}],
        "usage": _usage(result),
    }
    if "timings" in result:
        response["timings"] = result["timings"]
    return response


async def stream_chat_completion(
    lines: AsyncIterator[str],
    prompt: str,
    model: str,
    *,
    include_usage: bool,
) -> AsyncIterator[bytes]:
    """Translate /completion server-sent events into chat completion chunks."""

    completion_id = f"chatcmpl-{secrets.token_hex(12)}"
    created = int(time.time())
    splitter = ReasoningSplitter(prompt)

    def event(choices: list[dict[str, Any]], **extra: Any) -> bytes:
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": choices,
            **extra,
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode()

    def deltas(parts: list[tuple[str, str]]) -> Iterable[bytes]:
        for kind, text in parts:
            key = "reasoning_content" if kind == "reasoning" else "content"
            yield event([{"index": 0, "delta": {key: text}, "finish_reason": None}])

    yield event([{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])
    async for line in lines:
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        result = json.loads(data)
        if "error" in result:
            yield f"data: {json.dumps(result, ensure_ascii=False)}\n\n".encode()
            return
        for chunk in deltas(splitter.feed(str(result.get("content") or ""))):
            yield chunk
        if result.get("stop"):
            for chunk in deltas(splitter.finish()):
                yield chunk
            extra = {"timings": result["timings"]} if "timings" in result else {}
            yield event(
                [{"index": 0, "delta": {}, "finish_reason": _finish_reason(result)}], **extra
            )
            if include_usage:
                yield event([], usage=_usage(result))
            break
    yield b"data: [DONE]\n\n"


__all__ = [
    "THINK_FORMATS",
    "ReasoningSplitter",
    "chat_completion",
    "completion_request",
    "render_prompt",
    "stream_chat_completion",
    "supports",
    "template_kwargs",
]
