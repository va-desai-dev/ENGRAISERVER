"""The event log behind /control/events.

A snapshot answers "what is true now". This answers "what changed while I was
not looking", which is the question a phone in a pocket or a sleeping display
actually has. The cursor and epoch semantics are the contract a client depends
on, so they are pinned here rather than left to the endpoint's docstring.
"""

from __future__ import annotations

import json

import pytest

from engrai_server.events import MAX_LIMIT, EventLog, redact_paths


@pytest.fixture
def log() -> EventLog:
    return EventLog(capacity=5)


def test_ids_are_monotonic(log: EventLog) -> None:
    ids = [log.record("k", f"event {n}").id for n in range(4)]
    assert ids == [1, 2, 3, 4]


def test_a_cold_client_gets_recent_history_not_nothing(log: EventLog) -> None:
    """An app opening for the first time should show something."""
    for n in range(3):
        log.record("k", f"event {n}")
    page = log.since(None)
    assert [event["summary"] for event in page["events"]] == ["event 0", "event 1", "event 2"]


def test_a_cursor_returns_only_what_is_newer(log: EventLog) -> None:
    first = log.record("k", "one")
    log.record("k", "two")
    page = log.since(first.id)
    assert [event["summary"] for event in page["events"]] == ["two"]


def test_polling_with_the_returned_cursor_yields_nothing_new(log: EventLog) -> None:
    log.record("k", "one")
    page = log.since(None)
    assert log.since(page["cursor"])["events"] == []


def test_the_cursor_survives_an_empty_poll(log: EventLog) -> None:
    """An idle gateway must not reset a client to the start of the log."""
    log.record("k", "one")
    cursor = log.since(None)["cursor"]
    assert log.since(cursor)["cursor"] == cursor


def test_events_are_oldest_first(log: EventLog) -> None:
    for n in range(3):
        log.record("k", f"event {n}")
    ordered = [event["id"] for event in log.since(None)["events"]]
    assert ordered == sorted(ordered)


def test_the_log_is_bounded(log: EventLog) -> None:
    for n in range(20):
        log.record("k", f"event {n}")
    assert len(log.since(None, limit=MAX_LIMIT)["events"]) == 5


def test_a_client_that_fell_behind_is_told_so(log: EventLog) -> None:
    """Silently resuming would let a client splice a gap it cannot see."""
    stale = log.record("k", "one").id
    for n in range(10):
        log.record("k", f"event {n}")
    assert log.since(stale)["truncated"] is True


def test_a_client_that_kept_up_is_not_told_it_missed_anything(log: EventLog) -> None:
    log.record("k", "one")
    page = log.since(None)
    log.record("k", "two")
    assert log.since(page["cursor"])["truncated"] is False


def test_limit_is_clamped(log: EventLog) -> None:
    for n in range(5):
        log.record("k", f"event {n}")
    assert len(log.since(None, limit=0)["events"]) == 1
    assert len(log.since(None, limit=10_000)["events"]) == 5


def test_epoch_is_stable_within_a_process(log: EventLog) -> None:
    before = log.epoch
    log.record("k", "one")
    assert log.since(None)["epoch"] == before


def test_a_new_log_has_a_different_epoch() -> None:
    """This is how a client detects a restart and resets instead of appending."""
    assert EventLog().epoch != EventLog().epoch


def test_latest_reports_the_head_even_when_a_page_is_empty(log: EventLog) -> None:
    log.record("k", "one")
    last = log.record("k", "two").id
    assert log.since(last)["latest"] == last


# ── Path redaction ────────────────────────────────────────────────────────


def test_an_absolute_path_is_redacted(log: EventLog) -> None:
    """Failure summaries are built from upstream exception text.

    Those name the executable or the weights file, and the feed is readable
    with a display token that is meant to see no filesystem layout at all.
    """
    log.record("model.failed", "not found: /home/you/engrai-text-worker")
    assert log.since(None)["events"][0]["summary"] == "not found: <path>"


def test_a_cache_path_is_redacted(log: EventLog) -> None:
    log.record("model.failed", "missing /home/v/.cache/huggingface/hub/m/a.gguf here")
    assert "/home" not in log.since(None)["events"][0]["summary"]


def test_ordinary_text_survives_redaction() -> None:
    """Redaction must not mangle the summaries that carry no path at all."""
    assert redact_paths("Loading Alpha 31B") == "Loading Alpha 31B"
    assert redact_paths("GPU 0/1 busy") == "GPU 0/1 busy"
    assert redact_paths("--ctx-size 8192") == "--ctx-size 8192"


def test_redaction_cannot_be_bypassed_by_a_call_site(log: EventLog) -> None:
    """It happens inside record(), so no caller can forget it."""
    log.record("anything", "/etc/passwd/and/more", model="m")
    assert "/etc" not in log.since(None)["events"][0]["summary"]


def test_the_model_id_rides_along(log: EventLog) -> None:
    log.record("model.loaded", "Alpha is live", "alpha-q4")
    assert log.since(None)["events"][0]["model"] == "alpha-q4"


def test_records_from_many_threads_do_not_collide(log: EventLog) -> None:
    """FastAPI writes from a threadpool and the control plane from the loop."""
    import threading

    big = EventLog(capacity=1000)

    def spam() -> None:
        for _ in range(50):
            big.record("k", "x")

    threads = [threading.Thread(target=spam) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ids = [event["id"] for event in big.since(None, limit=MAX_LIMIT)["events"]]
    assert len(ids) == len(set(ids)) == 400


# ── The endpoint ──────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_API_KEYS", "eng-events-test-key")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(tmp_path / "commands"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text-worker.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("MODEL_SEARCH_ROOTS", str(tmp_path))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5999")

    import importlib

    from fastapi.testclient import TestClient

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    with TestClient(module.app) as instance:
        instance.headers.clear()
        yield instance
    get_settings.cache_clear()


def admin_token(client) -> str:
    response = client.post(
        "/control/session", json={"password": "a-long-enough-password"}
    )
    return response.json()["token"]


def display_token(client) -> str:
    response = client.post(
        "/control/display/tokens",
        json={"label": "feed"},
        headers={"Authorization": f"Bearer {admin_token(client)}"},
    )
    return response.json()["secret"]


def test_the_display_token_opens_the_feed(client) -> None:
    token = display_token(client)
    response = client.get(
        "/control/events", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200


def test_an_inference_key_does_not_open_the_feed(client) -> None:
    """Same boundary as the wall display: /v1 credentials stay out of /control."""
    response = client.get(
        "/control/events", headers={"Authorization": "Bearer eng-events-test-key"}
    )
    assert response.status_code == 401


def test_the_feed_needs_a_credential(client) -> None:
    assert client.get("/control/events").status_code == 401


def test_startup_is_recorded(client) -> None:
    token = display_token(client)
    body = client.get(
        "/control/events", headers={"Authorization": f"Bearer {token}"}
    ).json()
    assert any(event["kind"] == "gateway.started" for event in body["events"])


def test_a_failed_load_is_recorded_as_an_attempt_and_a_failure(client, tmp_path) -> None:
    """The interesting case for a feed: something went wrong while nobody looked.

    The executable does not exist, so the load cannot succeed. Both the attempt
    and the failure must appear, because a client that only ever sees successes
    cannot tell a broken runtime from an idle one.
    """
    admin = {"Authorization": f"Bearer {admin_token(client)}"}
    weights = tmp_path / "alpha-Q4_K_M.gguf"
    weights.write_bytes(b"gguf")
    saved = client.put(
        "/control/profiles/alpha",
        json={
            "name": "Alpha",
            "model_path": str(weights),
            "deployment": {"memory": {"context": 8192}},
            "notes": "",
        },
        headers=admin,
    )
    assert saved.status_code == 200, saved.text

    client.post("/control/models/alpha/load", headers=admin)

    token = display_token(client)
    body = client.get(
        "/control/events", headers={"Authorization": f"Bearer {token}"}
    ).json()
    kinds = [event["kind"] for event in body["events"]]
    assert "model.loading" in kinds
    assert "model.failed" in kinds
    failure = next(e for e in body["events"] if e["kind"] == "model.failed")
    assert failure["model"] == "alpha"


def test_the_feed_leaks_no_paths_or_arguments(client, tmp_path) -> None:
    """A feed is readable by a wall display; it must stay as narrow as one."""
    admin = {"Authorization": f"Bearer {admin_token(client)}"}
    weights = tmp_path / "alpha-Q4_K_M.gguf"
    weights.write_bytes(b"gguf")
    client.put(
        "/control/profiles/alpha",
        json={
            "name": "Alpha",
            "model_path": str(weights),
            "deployment": {"memory": {"context": 8192}},
            "notes": "",
        },
        headers=admin,
    )
    client.post("/control/models/alpha/load", headers=admin)

    token = display_token(client)
    raw = client.get(
        "/control/events", headers={"Authorization": f"Bearer {token}"}
    ).text
    assert str(weights) not in raw
    assert "--contextsize" not in raw
    # The failure summary is built from upstream exception text, which names
    # the executable. That is the leak this endpoint shipped with first.
    assert str(tmp_path) not in raw
    assert "/home/" not in raw


def test_typed_deployment_is_compiled_by_the_server(client, tmp_path) -> None:
    admin = {"Authorization": f"Bearer {admin_token(client)}"}
    weights = tmp_path / "typed-Q4_K_M.gguf"
    weights.write_bytes(b"gguf")
    response = client.put(
        "/control/profiles/typed",
        json={
            "name": "Typed",
            "model_path": str(weights),
            "prompt": {"template": "jinja", "adapter": "auto", "thinking": True},
            "deployment": {
                "compute": {
                    "backend": "cuda",
                    "devices": [0],
                    "gpu_layers": "all",
                    "tensor_split": [],
                    "flash_attention": True,
                },
                "memory": {"context": 16384, "kv_cache": "q8_0"},
                "generation": {"default_tokens": 1024, "reasoning_effort": "low"},
                "advanced": {"extra_arguments": []},
            },
        },
        headers=admin,
    )
    assert response.status_code == 200, response.text
    # The route catalog no longer persists a provider command.  The typed
    # deployment is compiled only when the owned llama.cpp worker launches.
    assert "arguments" not in response.json()["profile"]
    assert response.json()["profile"]["prompt"] == {
        "template": "jinja", "adapter": "auto", "thinking": True,
    }
    deployment = json.loads((tmp_path / "deployments/typed.json").read_text())
    assert deployment["engine"] == "llama.cpp"
    assert deployment["compute"]["backend"] == "cuda"
    assert deployment["memory"]["context"] == 16384
