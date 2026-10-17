from __future__ import annotations

import asyncio
import importlib
import json
import zlib
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from engrai_server.domain.store import DeploymentStore
from engrai_server.engines.llamacpp import chat
from engrai_server.events import EventLog
from engrai_server.ledger import Ledger, LedgerError
from engrai_server.ledger import recall
from engrai_server.ledger.embedder import (
    EmbeddingError,
    compile_embedding_launch,
    explain_launch_failure,
)
from engrai_server.ledger.index import VectorIndex
from engrai_server.ledger.models import LedgerEntry, LedgerSettings
from engrai_server.llamacpp import LlamaCppControlPlane
from engrai_server.profiles import ProfileStore
from engrai_server.settings import Settings


GEMMA4 = (Path(__file__).parent / "fixtures" / "gemma4-style-template.jinja").read_text(
    encoding="utf-8"
)
TODAY = date(2026, 10, 13)
DIMENSION = 96


class FakeEmbedder:
    """A bag-of-words encoder: texts sharing words score as similar."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = False
        self.stopped = False

    async def embed(self, texts, *, model_path, gpu_layers, timeout=120.0):  # type: ignore[no-untyped-def]
        if self.fail:
            raise EmbeddingError("embedding worker is down")
        self.calls.append(list(texts))
        vectors = []
        for text in texts:
            vector = [0.0] * DIMENSION
            for word in "".join(c.lower() if c.isalnum() else " " for c in text).split():
                if len(word) > 3:
                    vector[zlib.crc32(word.encode()) % DIMENSION] += 1.0
            vectors.append(vector if any(vector) else [1.0] + [0.0] * (DIMENSION - 1))
        return vectors

    async def status(self):  # type: ignore[no-untyped-def]
        return {"running": False, "model": None, "error": None}

    async def stop(self) -> None:
        self.stopped = True

    async def close(self) -> None:
        pass


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        control_api_keys="api-key",
        control_admin_token="admin-key",
        engine_host="127.0.0.1",
        text_engine_port=51112,
        route_dir=tmp_path / "commands",
        model_search_roots=str(tmp_path),
        model_state_path=tmp_path / "state.json",
        text_engine_log_path=tmp_path / "worker.log",
        ledger_data_dir=tmp_path / "ledger",
    )


async def make_ledger(
    tmp_path: Path, *, routes: tuple[str, ...] = ("gemma",), **overrides: object
) -> tuple[Ledger, FakeEmbedder]:
    model = tmp_path / "embed.gguf"
    model.write_bytes(b"GGUF")
    embedder = FakeEmbedder()
    ledger = Ledger(
        make_settings(tmp_path), embedder=embedder, events=EventLog(), today=lambda: TODAY
    )
    await ledger.save_settings(
        {
            "subject_name": "Vee",
            "enabled_routes": list(routes),
            "embedding_model_path": str(model),
            "min_score": 0.2,
            **overrides,
        }
    )
    return ledger, embedder


async def settle(ledger: Ledger) -> None:
    if ledger._index_task is not None:
        await ledger._index_task


EPISODES = [
    {"title": "Moved to Muncie", "when": "2019-08", "content": "Relocated to Muncie for graduate school."},
    {"title": "Marathon", "when": "2023-05-07", "content": "Finished the Indianapolis marathon in the rain."},
    {"title": "First telescope", "when": "2011", "content": "Built a telescope and watched Saturn from the roof."},
]


async def seeded(tmp_path: Path, **overrides: object) -> tuple[Ledger, FakeEmbedder]:
    ledger, embedder = await make_ledger(tmp_path, **overrides)
    await ledger.save_entry(
        {"title": "Diet", "content": "Vegetarian since childhood.", "pinned": True}
    )
    for episode in EPISODES:
        await ledger.save_entry(dict(episode))
    await settle(ledger)
    return ledger, embedder


# ── Records ──────────────────────────────────────────────────────────────


def test_partial_dates_sort_chronologically_and_undated_entries_come_last() -> None:
    entries = [
        LedgerEntry(title="undated", content="x"),
        LedgerEntry(title="day", content="x", when="2019-06-14"),
        LedgerEntry(title="year", content="x", when="2019"),
        LedgerEntry(title="month", content="x", when="2019-06"),
        LedgerEntry(title="early", content="x", when="1998-12"),
    ]

    assert [entry.title for entry in recall.chronological(entries)] == [
        "early", "year", "month", "day", "undated",
    ]


@pytest.mark.parametrize("when", ["19", "2019-13", "2019-02-30", "June 2019", "2019/06"])
def test_malformed_dates_are_rejected(when: str) -> None:
    with pytest.raises(ValidationError):
        LedgerEntry(title="t", content="c", when=when)


def test_a_span_must_run_forwards_and_blank_dates_mean_undated() -> None:
    assert LedgerEntry(title="t", content="c", when=" ", until="").when is None
    assert LedgerEntry(title="t", content="c", when="2021", until="2023").date_label == "2021 – 2023"
    with pytest.raises(ValidationError):
        LedgerEntry(title="t", content="c", when="2023", until="2021")
    with pytest.raises(ValidationError):
        LedgerEntry(title="t", content="c", until="2021")


def test_settings_reject_relative_models_and_malformed_routes() -> None:
    with pytest.raises(ValidationError):
        LedgerSettings(embedding_model_path="models/embed.gguf")
    with pytest.raises(ValidationError):
        LedgerSettings(enabled_routes=["../escape"])
    assert LedgerSettings(enabled_routes=["b", "a", "b"]).enabled_routes == ["a", "b"]


# ── Index ────────────────────────────────────────────────────────────────


def test_index_ranks_by_cosine_and_survives_a_reload(tmp_path: Path) -> None:
    index = VectorIndex(tmp_path / "index.json")
    index.reset("model-a")
    index.put("near", "near text", [1.0, 0.1, 0.0])
    index.put("far", "far text", [0.0, 0.2, 1.0])
    index.put("opposite", "opposite text", [-1.0, 0.0, 0.0])
    index.save()

    reloaded = VectorIndex(tmp_path / "index.json")
    reloaded.load()
    hits = reloaded.search([1.0, 0.0, 0.0], ["near", "far", "opposite"], top_k=5, min_score=0.0)

    assert [entry_id for entry_id, _ in hits] == ["near", "far"]
    assert hits[0][1] == pytest.approx(0.995, abs=1e-3)
    assert reloaded.fingerprint == "model-a" and reloaded.dimension == 3
    assert reloaded.current("near", "near text") and not reloaded.current("near", "edited")
    assert (tmp_path / "index.json").stat().st_mode & 0o777 == 0o600


def test_index_only_searches_the_candidates_it_is_given(tmp_path: Path) -> None:
    index = VectorIndex(tmp_path / "index.json")
    index.put("a", "a", [1.0, 0.0])
    index.put("b", "b", [1.0, 0.0])

    assert index.search([1.0, 0.0], ["b", "missing"], top_k=5, min_score=0.0) == [("b", 1.0)]
    assert index.search([1.0, 0.0, 0.0], ["a"], top_k=5, min_score=0.0) == []
    assert index.prune(["a"]) and list(index.rows) == ["a"]


def test_a_corrupt_index_starts_empty(tmp_path: Path) -> None:
    path = tmp_path / "index.json"
    path.write_text("{not json", encoding="utf-8")
    index = VectorIndex(path)
    index.load()

    assert index.rows == {} and index.dimension == 0


# ── Prompt placement ─────────────────────────────────────────────────────


def test_pinned_merges_into_the_clients_system_turn() -> None:
    messages = [{"role": "system", "content": "You are Mira."}, {"role": "user", "content": "hi"}]

    updated = recall.add_pinned(messages, "[Ledger]")

    assert updated[0] == {"role": "system", "content": "[Ledger]\n\nYou are Mira."}
    assert len(updated) == 2 and messages[0]["content"] == "You are Mira."


def test_pinned_becomes_the_system_turn_when_the_client_sent_none() -> None:
    updated = recall.add_pinned([{"role": "user", "content": "hi"}], "[Ledger]")

    assert updated[0] == {"role": "system", "content": "[Ledger]"}
    assert updated[1] == {"role": "user", "content": "hi"}


def test_recalled_lands_immediately_before_the_newest_user_turn() -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
        {"role": "assistant", "content": "prefill"},
    ]

    as_system = recall.add_recalled(messages, "[Recalled]", "system")
    as_user = recall.add_recalled(messages, "[Recalled]", "user")

    assert [m["role"] for m in as_system] == ["user", "assistant", "system", "user", "assistant"]
    assert as_system[2]["content"] == "[Recalled]"
    assert as_user[2]["content"] == "[Recalled]\n\nsecond" and len(as_user) == 4


def test_recalled_folds_into_multimodal_content_as_a_text_part() -> None:
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}
    messages = [{"role": "user", "content": [{"type": "text", "text": "what is this?"}, image]}]

    updated = recall.add_recalled(messages, "[Recalled]", "user")

    assert updated[0]["content"][0] == {"type": "text", "text": "[Recalled]\n\n"}
    assert updated[0]["content"][2] == image


def test_the_query_is_the_last_two_user_turns() -> None:
    messages = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "the marathon"},
        {"role": "assistant", "content": "ignored"},
        {"role": "user", "content": [{"type": "text", "text": "and after that?"}]},
    ]

    assert recall.query_text(messages) == "the marathon\nand after that?"


def test_blocks_are_chronological_and_dated() -> None:
    entries = [LedgerEntry(**episode) for episode in EPISODES]

    block = recall.recalled_block(entries, "Vee", "2026-10-13")

    lines = block.splitlines()
    assert "Vee" in lines[0] and "Today is 2026-10-13" in lines[0]
    assert lines[1].startswith("- 2011 · First telescope:")
    assert lines[2].startswith("- 2019-08 · Moved to Muncie:")
    assert lines[3].startswith("- 2023-05-07 · Marathon:")
    assert recall.recalled_block([], "Vee", "2026-10-13") == ""
    assert "the user" in recall.pinned_block(entries[:1], "")


def test_the_budget_keeps_the_best_ranked_entries_that_fit() -> None:
    long = LedgerEntry(title="long", content="x" * 4000)
    short = LedgerEntry(title="short", content="fits")

    assert recall.within_budget([long, short], 100) == [short]
    assert recall.within_budget([short, long], 2000) == [short, long]


# ── Service ──────────────────────────────────────────────────────────────


async def test_recall_finds_the_relevant_episode_and_skips_pinned_entries(tmp_path: Path) -> None:
    ledger, _ = await seeded(tmp_path)

    hits = await ledger.recall("How did the marathon in Indianapolis go?")

    assert [entry.title for entry, _ in hits][0] == "Marathon"
    assert "Diet" not in [entry.title for entry, _ in hits]
    assert all(0.2 <= score <= 1.0001 for _, score in hits)


async def test_saving_an_entry_embeds_only_what_changed(tmp_path: Path) -> None:
    ledger, embedder = await seeded(tmp_path)
    embedder.calls.clear()
    marathon = next(e for e in ledger.entries.values() if e.title == "Marathon")

    await ledger.save_entry({**marathon.model_dump(), "content": "Ran it again, this time dry."})
    await settle(ledger)

    assert len(embedder.calls) == 1 and len(embedder.calls[0]) == 1
    assert "Ran it again" in embedder.calls[0][0]
    await ledger.save_entry({**marathon.model_dump(), "content": "Ran it again, this time dry."})
    await settle(ledger)
    assert len(embedder.calls) == 1


async def test_changing_the_embedding_model_rebuilds_the_whole_index(tmp_path: Path) -> None:
    ledger, embedder = await seeded(tmp_path)
    embedder.calls.clear()
    other = tmp_path / "other-embed.gguf"
    other.write_bytes(b"GGUF2")

    await ledger.save_settings(
        {**ledger.settings.model_dump(), "embedding_model_path": str(other)}
    )
    await settle(ledger)

    assert sum(len(call) for call in embedder.calls) == 4
    assert str(other) in ledger.index.fingerprint


async def test_document_and_query_prefixes_reach_the_embedder(tmp_path: Path) -> None:
    ledger, embedder = await seeded(
        tmp_path, document_prefix="search_document: ", query_prefix="search_query: "
    )

    assert all(text.startswith("search_document: ") for call in embedder.calls for text in call)
    embedder.calls.clear()
    await ledger.recall("marathon")
    assert embedder.calls == [["search_query: marathon"]]


async def test_a_title_placeholder_puts_the_heading_inside_the_prefix(tmp_path: Path) -> None:
    ledger, embedder = await seeded(tmp_path, document_prefix="title: {title} | text: ")

    documents = [text for call in embedder.calls for text in call]

    assert "title: Marathon (2023-05-07) | text: Finished the Indianapolis marathon in the rain." in documents
    assert "title: Diet | text: Vegetarian since childhood." in documents


async def test_removed_and_disabled_entries_are_never_recalled(tmp_path: Path) -> None:
    ledger, _ = await seeded(tmp_path)
    marathon = next(e for e in ledger.entries.values() if e.title == "Marathon")
    telescope = next(e for e in ledger.entries.values() if e.title == "First telescope")

    await ledger.save_entry({**marathon.model_dump(), "enabled": False})
    await ledger.remove_entry(str(telescope.id))
    await settle(ledger)

    titles = [entry.title for entry, _ in await ledger.recall("marathon telescope Saturn Muncie")]
    assert titles == ["Moved to Muncie"]
    assert str(telescope.id) not in ledger.index.rows


async def test_the_ledger_reloads_from_disk(tmp_path: Path) -> None:
    first, _ = await seeded(tmp_path)
    second = Ledger(make_settings(tmp_path), embedder=FakeEmbedder(), today=lambda: TODAY)

    snapshot = await second.snapshot()

    assert [entry["title"] for entry in snapshot["entries"]] == [
        "First telescope", "Moved to Muncie", "Marathon", "Diet",
    ]
    assert snapshot["settings"]["subject_name"] == "Vee"
    assert snapshot["status"]["indexed"] == 4 and snapshot["status"]["unindexed"] == 0
    assert (tmp_path / "ledger" / "entries.json").stat().st_mode & 0o777 == 0o600
    assert first.settings == second.settings


async def test_invalid_entries_are_refused_with_a_readable_reason(tmp_path: Path) -> None:
    ledger, _ = await make_ledger(tmp_path)

    with pytest.raises(LedgerError, match="when: dates look like"):
        await ledger.save_entry({"title": "t", "content": "c", "when": "last spring"})
    with pytest.raises(LedgerError):
        await ledger.save_entry({"title": "t", "content": "c", "surprise": 1})
    with pytest.raises(KeyError):
        await ledger.remove_entry("missing")


# ── Injection ────────────────────────────────────────────────────────────


def chat_payload(*turns: str) -> dict[str, object]:
    roles = ("user", "assistant")
    return {
        "model": "gemma",
        "messages": [
            {"role": "system", "content": "You are Mira."},
            *({"role": roles[i % 2], "content": turn} for i, turn in enumerate(turns)),
        ],
    }


async def test_augment_adds_pinned_first_and_recall_before_the_question(tmp_path: Path) -> None:
    ledger, _ = await seeded(tmp_path)
    payload = chat_payload("Tell me about the marathon in Indianapolis.")

    await ledger.augment("gemma", payload, rendered=True)

    messages = payload["messages"]
    assert [m["role"] for m in messages] == ["system", "system", "user"]  # type: ignore[index]
    assert messages[0]["content"].startswith("[Ledger: standing background about Vee.")  # type: ignore[index]
    assert "Vegetarian since childhood." in messages[0]["content"]  # type: ignore[index]
    assert messages[0]["content"].endswith("You are Mira.")  # type: ignore[index]
    assert "2023-05-07 · Marathon" in messages[1]["content"]  # type: ignore[index]
    assert "Today is 2026-10-13" in messages[1]["content"]  # type: ignore[index]


async def test_routes_that_are_not_enabled_are_left_untouched(tmp_path: Path) -> None:
    ledger, embedder = await seeded(tmp_path, routes=("other",))
    embedder.calls.clear()
    payload = chat_payload("Tell me about the marathon.")
    before = json.dumps(payload)

    await ledger.augment("gemma", payload, rendered=True)

    assert json.dumps(payload) == before and embedder.calls == []


async def test_the_native_handler_gets_recall_inside_the_user_turn(tmp_path: Path) -> None:
    ledger, _ = await seeded(tmp_path)
    payload = chat_payload("Tell me about the marathon in Indianapolis.")

    await ledger.augment("gemma", payload, rendered=False)

    messages = payload["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]  # type: ignore[index]
    assert messages[1]["content"].startswith("[Ledger: episodes")  # type: ignore[index]
    assert messages[1]["content"].endswith("Tell me about the marathon in Indianapolis.")  # type: ignore[index]


async def test_recall_failure_sends_pinned_only_and_backs_off(tmp_path: Path) -> None:
    ledger, embedder = await seeded(tmp_path)
    embedder.fail = True
    payload = chat_payload("Tell me about the marathon.")

    await ledger.augment("gemma", payload, rendered=True)

    messages = payload["messages"]
    assert [m["role"] for m in messages] == ["system", "user"]  # type: ignore[index]
    assert "Vegetarian since childhood." in messages[0]["content"]  # type: ignore[index]
    events = ledger.events.since(None)["events"]  # type: ignore[union-attr]
    assert [event["kind"] for event in events] == ["ledger.recall_failed"]
    assert "marathon" not in events[0]["summary"].lower()

    embedder.fail = False
    again = chat_payload("Tell me about the marathon.")
    await ledger.augment("gemma", again, rendered=True)
    assert len(again["messages"]) == 2  # type: ignore[arg-type]
    assert len(ledger.events.since(None)["events"]) == 1  # type: ignore[union-attr]


async def test_a_broken_ledger_never_fails_the_chat_request(tmp_path: Path) -> None:
    ledger, _ = await seeded(tmp_path)
    ledger.entries = None  # type: ignore[assignment]
    payload = chat_payload("hello")
    before = json.dumps(payload)

    await ledger.augment("gemma", payload, rendered=True)

    assert json.dumps(payload) == before


async def test_without_an_embedding_model_the_pinned_block_still_works(tmp_path: Path) -> None:
    ledger, embedder = await make_ledger(tmp_path, embedding_model_path="")
    await ledger.save_entry({"title": "Diet", "content": "Vegetarian.", "pinned": True})
    await ledger.save_entry(dict(EPISODES[0]))
    payload = chat_payload("Tell me about Muncie.")

    await ledger.augment("gemma", payload, rendered=True)

    assert [m["role"] for m in payload["messages"]] == ["system", "user"]  # type: ignore[index]
    assert embedder.calls == [] and ledger._index_task is None


async def test_the_prompt_prefix_stays_stable_across_turns(tmp_path: Path) -> None:
    """Recall changes per turn, yet only the newest exchange is re-read.

    Everything before the previous user turn, pinned block included, renders
    byte for byte the same, so the worker's prompt cache still covers it.
    """

    ledger, _ = await seeded(tmp_path)
    history = ("Tell me about the marathon in Indianapolis.", "It rained the whole way.")
    second = chat_payload(*history, "What about the telescope and Saturn?")
    third = chat_payload(
        *history, "What about the telescope and Saturn?", "You built it in 2011.",
        "And when did I move to Muncie?",
    )
    await ledger.augment("gemma", second, rendered=True)
    await ledger.augment("gemma", third, rendered=True)
    assert "First telescope" in second["messages"][3]["content"]  # type: ignore[index]
    assert "Moved to Muncie" in third["messages"][5]["content"]  # type: ignore[index]

    two = chat.render_prompt(GEMMA4, second["messages"])  # type: ignore[arg-type]
    three = chat.render_prompt(GEMMA4, third["messages"])  # type: ignore[arg-type]

    common = next(i for i, (a, b) in enumerate(zip(two, three)) if a != b)
    kept = two[:common]
    assert "Vegetarian since childhood." in kept and "You are Mira." in kept
    assert "It rained the whole way." in kept
    assert "[Ledger: episodes" not in kept


# ── Through the control plane ────────────────────────────────────────────


class OneChunkStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self.content = content

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        yield self.content


def control_plane(tmp_path: Path, handler, ledger: Ledger) -> LlamaCppControlPlane:  # type: ignore[no-untyped-def]
    model = tmp_path / "gemma.gguf"
    model.write_bytes(b"GGUF")
    profiles = ProfileStore(tmp_path / "commands", [tmp_path])
    profiles.save(
        "gemma",
        name="Gemma",
        model_path=str(model),
        prompt={"template": "jinja", "adapter": "auto", "thinking": False},
    )
    control = LlamaCppControlPlane(
        make_settings(tmp_path), profiles, DeploymentStore(tmp_path / "deployments"), ledger
    )
    control.client = httpx.AsyncClient(
        base_url="http://worker.test", transport=httpx.MockTransport(handler)
    )
    return control


async def run(control: LlamaCppControlPlane, request: dict[str, object], **options: bool) -> None:
    _, stream = await control.forward_openai(
        "POST", "chat/completions", json.dumps(request).encode(), "", "application/json", **options
    )
    [chunk async for chunk in stream]
    await control.client.aclose()


async def test_rendered_chat_carries_the_ledger_to_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = await seeded(tmp_path)
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"chat_template": GEMMA4})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"content": "ok", "stop": True})

    control = control_plane(tmp_path, handler, ledger)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))

    await run(control, chat_payload("Tell me about the marathon in Indianapolis."))

    path, body = seen[0]
    prompt = str(body["prompt"])
    assert path == "/completion"
    assert prompt.index("Vegetarian since childhood.") < prompt.index("You are Mira.")
    assert prompt.index("You are Mira.") < prompt.index("2023-05-07 · Marathon")
    assert prompt.index("2023-05-07 · Marathon") < prompt.index("Tell me about the marathon")


async def test_tool_requests_carry_the_ledger_to_the_native_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = await seeded(tmp_path)
    seen: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, stream=OneChunkStream(b'{"ok": true}'))

    control = control_plane(tmp_path, handler, ledger)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))
    request = chat_payload("Tell me about the marathon in Indianapolis.")
    request["tools"] = [{"type": "function", "function": {"name": "weather"}}]

    await run(control, request)

    path, body = seen[0]
    messages = body["messages"]
    assert path == "/v1/chat/completions"
    assert [m["role"] for m in messages] == ["system", "user"]  # type: ignore[union-attr]
    assert "Vegetarian" in messages[0]["content"]  # type: ignore[index]
    assert "Marathon" in messages[1]["content"]  # type: ignore[index]


async def test_internal_callers_can_keep_the_ledger_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = await seeded(tmp_path)
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/props":
            return httpx.Response(200, json={"chat_template": GEMMA4})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"content": "ok", "stop": True})

    control = control_plane(tmp_path, handler, ledger)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))

    await run(control, chat_payload("Tell me about the marathon."), ledger=False)

    assert "Ledger" not in str(seen[0]["prompt"])


async def test_raw_completions_are_never_touched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger, _ = await seeded(tmp_path)
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, stream=OneChunkStream(b'{"ok": true}'))

    control = control_plane(tmp_path, handler, ledger)
    monkeypatch.setattr(control, "ensure_model", AsyncMock(return_value=False))
    _, stream = await control.forward_openai(
        "POST",
        "completions",
        json.dumps({"model": "gemma", "prompt": "the marathon"}).encode(),
        "",
        "application/json",
    )
    [chunk async for chunk in stream]
    await control.client.aclose()

    assert seen == [{"model": "gemma", "prompt": "the marathon"}]


# ── Embedding worker launch ──────────────────────────────────────────────


def test_the_embedding_worker_is_a_loopback_cpu_encoder_by_default(tmp_path: Path) -> None:
    launch = compile_embedding_launch(
        tmp_path / "engrai-text-worker", tmp_path / "embed.gguf",
        host="127.0.0.1", port=5004, gpu_layers=0,
    )
    arguments = list(launch.arguments)

    assert "--embedding" in arguments and "--offline" in arguments and "--no-ui" in arguments
    assert arguments[arguments.index("--device") + 1] == "none"
    assert arguments[arguments.index("--port") + 1] == "5004"
    assert arguments[arguments.index("--ubatch-size") + 1] == "2048"

    offloaded = compile_embedding_launch(
        tmp_path / "w", tmp_path / "e.gguf", host="127.0.0.1", port=5004, gpu_layers=99
    )
    assert "--device" not in offloaded.arguments
    assert offloaded.arguments[offloaded.arguments.index("--gpu-layers") + 1] == "99"


def test_an_unsupported_encoder_is_explained_without_the_worker_log() -> None:
    log = (
        "llama.cpp-embedding exited with status 1:\n"
        "0.00.033.177 I srv    load_model: loading model '/models/e.gguf'\n"
        "0.00.158.106 E llama_model_load: error loading model: unknown model architecture: "
        "'gemma-embedding2'\n"
        "0.00.271.608 E srv  llama_server: exiting due to model loading error"
    )

    assert explain_launch_failure(log) == (
        "This ENGRAI text runtime cannot load 'gemma-embedding2' models. "
        "Choose an embedding model its llama.cpp supports, or install a newer runtime."
    )
    assert explain_launch_failure(
        "llama.cpp-embedding exited with status 1:\n0.01 E srv  load_model: out of memory"
    ) == "The embedding model did not load: srv  load_model: out of memory"
    assert explain_launch_failure("127.0.0.1:5004 is occupied").endswith("127.0.0.1:5004 is occupied")


def test_the_embedding_lane_follows_a_relocated_text_worker(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    assert settings.embedding_engine_port == 51114 and settings.image_engine_port == 51113
    assert settings.embedding_engine_log_path == tmp_path / "embedding-worker.log"


# ── Control routes ───────────────────────────────────────────────────────


@pytest.fixture
def console(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_API_KEYS", "ledger-api-key")
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
    monkeypatch.setenv("ENGRAI_LEDGER_DATA_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5938")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    with TestClient(module.app) as client:
        token, _ = module.credentials.create_session()
        yield client, {"Authorization": f"Bearer {token}"}
    get_settings.cache_clear()


def test_ledger_routes_need_the_admin_session(console) -> None:  # type: ignore[no-untyped-def]
    client, _ = console

    assert client.get("/control/ledger").status_code == 401
    assert (
        client.get("/control/ledger", headers={"Authorization": "Bearer ledger-api-key"}).status_code
        == 401
    )


def test_entries_and_settings_round_trip_through_the_console(console) -> None:  # type: ignore[no-untyped-def]
    client, auth = console

    empty = client.get("/control/ledger", headers=auth).json()
    assert empty["entries"] == [] and empty["settings"]["enabled_routes"] == []

    saved = client.post(
        "/control/ledger/entries",
        headers=auth,
        json={"title": "Diet", "content": "Vegetarian.", "pinned": True},
    ).json()
    entry_id = saved["entry"]["id"]
    assert saved["ok"] and saved["status"]["pinned"] == 1 and saved["status"]["pinned_tokens"] > 0

    client.post(
        "/control/ledger/entries",
        headers=auth,
        json={"title": "Moved", "content": "To Muncie.", "when": "2019-08"},
    )
    listed = client.get("/control/ledger", headers=auth).json()
    assert [entry["title"] for entry in listed["entries"]] == ["Moved", "Diet"]

    settings = client.put(
        "/control/ledger/settings",
        headers=auth,
        json={**listed["settings"], "subject_name": "Vee", "enabled_routes": ["gemma"]},
    ).json()
    assert settings["settings"]["enabled_routes"] == ["gemma"]

    preview = client.post("/control/ledger/preview", headers=auth, json={"text": "Muncie"}).json()
    assert "Vegetarian." in preview["pinned_block"] and "Vee" in preview["pinned_block"]
    assert preview["recalled"] == [] and preview["error"] is None

    assert client.delete(f"/control/ledger/entries/{entry_id}", headers=auth).json()["ok"]
    assert client.delete(f"/control/ledger/entries/{entry_id}", headers=auth).status_code == 404


def test_bad_ledger_input_is_a_400_with_the_reason(console) -> None:  # type: ignore[no-untyped-def]
    client, auth = console

    bad_date = client.post(
        "/control/ledger/entries", headers=auth, json={"title": "t", "content": "c", "when": "soon"}
    )
    bad_model = client.put(
        "/control/ledger/settings", headers=auth, json={"embedding_model_path": "embed.gguf"}
    )

    assert bad_date.status_code == 400 and "dates look like" in bad_date.json()["detail"]
    assert bad_model.status_code == 400 and "absolute .gguf" in bad_model.json()["detail"]
    assert client.post("/control/ledger/preview", headers=auth, json={}).status_code == 400


async def test_closing_the_ledger_cancels_indexing(tmp_path: Path) -> None:
    ledger, embedder = await make_ledger(tmp_path)
    gate = asyncio.Event()

    async def slow(texts, **_):  # type: ignore[no-untyped-def]
        await gate.wait()
        return []

    embedder.embed = slow  # type: ignore[method-assign]
    await ledger.save_entry(dict(EPISODES[0]))
    await asyncio.sleep(0)

    await ledger.close()

    assert ledger._index_task is not None and ledger._index_task.done()
