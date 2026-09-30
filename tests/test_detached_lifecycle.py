"""Starting and stopping the worker without holding the request open.

Either operation can take the better part of a minute — a load waits for
weights and CUDA, an unload waits out SIGTERM before it resorts to SIGKILL —
and the caller is not always a browser tab someone is watching. An iOS App
Intent runs on an execution budget, and a Shortcut fired from a widget or
from Siri has no way to block that long; a reverse proxy in front of the
gateway has its own idle timeout that a cold start can outlast. So both
endpoints grew a second mode.

The properties pinned here are the ones a client depends on: the waited mode
is unchanged, the detached mode answers immediately and names what it is
doing, an already-live profile is never torn down to satisfy either, only one
lifecycle operation runs at a time, and the event feed reads identically
whichever mode produced it — because a client watching the feed has no other
way to learn how a detached operation ended.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

import httpx
import pytest

from engrai_server.profiles import ProfileStore


AUTH = {"Authorization": "Bearer a-long-enough-password"}


@pytest.fixture
def module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    commands = tmp_path / "commands"
    store = ProfileStore(commands, [])
    for profile_id, label in (("alpha", "Alpha"), ("beta", "Beta")):
        weights = tmp_path / f"{profile_id}-Q4_K_M.gguf"
        weights.write_bytes(b"gguf")
        store.save(
            profile_id,
            name=label,
            model_path=str(weights),
        )

    monkeypatch.setenv("CONTROL_API_KEYS", "probe-key-000000")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(commands))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text-worker.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    # Nothing listens here, so the engine probe fails fast rather than
    # reaching a real runtime on the developer's machine.
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5999")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    yield importlib.reload(engrai_server.main)
    get_settings.cache_clear()


@pytest.fixture
async def client(module):
    """Drives the app in the test's own loop.

    TestClient would run it in a portal thread, which makes "a load is still
    in flight while the next request arrives" a matter of timing rather than
    of control. Here the gate below decides when the load finishes.
    """
    transport = httpx.ASGITransport(app=module.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as http:
        yield http
    await module.control.close()


@pytest.fixture
def gated_load(module):
    """Replaces the real load with one the test opens by hand."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_load(profile):
        started.set()
        await release.wait()
        return True

    module.control.load = fake_load
    return started, release


@pytest.fixture
def gated_unload(module):
    """The unload equivalent of gated_load."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def fake_unload():
        started.set()
        await release.wait()

    module.control.unload = fake_unload
    return started, release


async def events_since(
    client: httpx.AsyncClient, module, cursor: int | None = None
) -> list[dict]:
    """Read the feed the way a client does.

    The feed sits behind require_display, which takes a display token or a
    session — not the admin password the write routes accept directly. So a
    session is minted here rather than reusing AUTH, which would 401.
    """
    token, _ = module.credentials.create_session()
    query = "" if cursor is None else f"?since={cursor}"
    response = await client.get(
        f"/control/events{query}", headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 200
    return response.json()["events"]


async def test_waiting_is_still_the_default(client, module) -> None:
    """The web UI's Load button must behave exactly as it did."""

    async def immediate(profile):
        return True

    module.control.load = immediate

    response = await client.post("/control/models/alpha/load", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["changed"] is True
    # The load is over by the time this returns, so nothing is in flight.
    assert body["accepted"] is False
    assert module.control.background_operation() is None


async def test_detached_load_answers_before_it_finishes(client, module, gated_load) -> None:
    started, release = gated_load

    response = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert response.status_code == 202
    assert response.json()["accepted"] is True

    await asyncio.wait_for(started.wait(), timeout=2)
    # Answered, and the work demonstrably has not finished.
    assert module.control.background_operation() == {"kind": "load", "model": "alpha"}

    release.set()
    await module.control.background_task
    assert module.control.background_operation() is None


async def test_a_detached_load_names_its_target_immediately(client, module, gated_load) -> None:
    """The runtime phase lags by a tick; the snapshot must not.

    A client that polls the instant it sees a 202 would otherwise read the
    phase belonging to the previous model and conclude nothing happened.
    """
    started, release = gated_load

    response = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert response.json()["state"]["background"] == {"kind": "load", "model": "alpha"}

    await asyncio.wait_for(started.wait(), timeout=2)
    release.set()
    await module.control.background_task


async def test_a_live_model_is_not_reloaded(
    client, module, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refusing to wait is not a reason to tear down a working worker."""
    calls: list[str] = []

    async def fake_load(profile):
        calls.append(profile.id)
        return True

    module.control.load = fake_load
    # is_live is faked rather than the process state behind it. A stub process
    # would hand teardown a real PID to signal, and faking _owned_running
    # alone leaves engine_status dereferencing a process that is not there.
    # What this test is about is the endpoint honouring the answer.
    monkeypatch.setattr(module.control, "is_live", lambda profile_id: profile_id == "alpha")

    response = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert response.json()["changed"] is False
    assert calls == []
    assert module.control.background_operation() is None


async def test_a_second_target_is_refused_while_one_is_in_flight(
    client, module, gated_load
) -> None:
    started, release = gated_load

    first = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert first.status_code == 202
    await asyncio.wait_for(started.wait(), timeout=2)

    second = await client.post("/control/models/beta/load?wait=false", headers=AUTH)
    assert second.status_code == 409
    assert "alpha" in second.json()["detail"]

    release.set()
    await module.control.background_task


async def test_asking_twice_for_the_same_model_starts_one_load(
    client, module, gated_load
) -> None:
    """A retried Shortcut must not queue a second load of what is already coming."""
    started, release = gated_load

    first = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    await asyncio.wait_for(started.wait(), timeout=2)
    task = module.control.background_task

    second = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["accepted"] is False
    assert module.control.background_task is task

    release.set()
    await task


async def test_a_detached_failure_still_reaches_the_feed(client, module) -> None:
    """The property the whole mode rests on.

    Nobody is holding the request that started this, so the feed is the only
    place the failure can be reported. If it is lost here, a phone waiting on
    a terminal event waits forever.
    """

    async def failing(profile):
        raise RuntimeError("CUDA runtime not found")

    module.control.load = failing

    response = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert response.status_code == 202
    await module.control.background_task

    feed = await events_since(client, module)
    failures = [event for event in feed if event["kind"] == "model.failed"]
    assert failures, feed
    assert "CUDA runtime not found" in failures[-1]["summary"]
    assert failures[-1]["model"] == "alpha"


async def test_both_modes_write_the_same_history(client, module) -> None:
    """A client reading the feed cannot tell which mode produced an entry.

    That is what lets one event handler serve a browser and a phone, so it is
    asserted rather than left to two call sites that happen to agree today.
    """

    async def immediate(profile):
        return True

    module.control.load = immediate

    await client.post("/control/models/alpha/load", headers=AUTH)
    waited = await events_since(client, module)
    cursor = waited[-1]["id"]

    await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    await module.control.background_task
    detached = await events_since(client, module, cursor)

    def shape(events: list[dict]) -> list[tuple[str, str, str | None]]:
        return [(event["kind"], event["summary"], event["model"]) for event in events]

    assert shape(detached) == shape(waited[-2:])


async def test_shutdown_does_not_record_a_failure(client, module, gated_load) -> None:
    """Cancellation is not a failed load and must not read as one."""
    started, release = gated_load

    await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    await asyncio.wait_for(started.wait(), timeout=2)

    await module.control.cancel_background_operation()

    feed = await events_since(client, module)
    assert not [event for event in feed if event["kind"] == "model.failed"], feed


async def test_unload_waits_by_default(client, module) -> None:
    """The UI's Unload button must behave exactly as it did."""
    unloaded: list[bool] = []

    async def immediate():
        unloaded.append(True)

    module.control.unload = immediate

    response = await client.post("/control/models/unload", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["accepted"] is False
    assert unloaded == [True]
    assert module.control.background_operation() is None


async def test_detached_unload_answers_before_it_finishes(
    client, module, gated_unload
) -> None:
    started, release = gated_unload

    response = await client.post("/control/models/unload?wait=false", headers=AUTH)
    assert response.status_code == 202
    assert response.json()["accepted"] is True

    await asyncio.wait_for(started.wait(), timeout=2)
    assert module.control.background_operation() == {"kind": "unload", "model": None}

    release.set()
    await module.control.background_task
    assert module.control.background_operation() is None


async def test_a_detached_unload_names_the_model_it_is_releasing(
    client, module, gated_unload
) -> None:
    """So a phone can say what it is unloading, not just that it is busy."""
    started, release = gated_unload
    module.control.state.active_model = "alpha"

    response = await client.post("/control/models/unload?wait=false", headers=AUTH)
    assert response.json()["state"]["background"] == {"kind": "unload", "model": "alpha"}

    await asyncio.wait_for(started.wait(), timeout=2)
    release.set()
    await module.control.background_task


async def test_unload_is_refused_while_a_load_is_in_flight(
    client, module, gated_load
) -> None:
    """One slot: the worker cannot be started and stopped at the same time."""
    started, release = gated_load

    await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    await asyncio.wait_for(started.wait(), timeout=2)

    refused = await client.post("/control/models/unload?wait=false", headers=AUTH)
    assert refused.status_code == 409
    assert "alpha" in refused.json()["detail"]

    release.set()
    await module.control.background_task


async def test_load_is_refused_while_an_unload_is_in_flight(
    client, module, gated_unload
) -> None:
    started, release = gated_unload

    await client.post("/control/models/unload?wait=false", headers=AUTH)
    await asyncio.wait_for(started.wait(), timeout=2)

    refused = await client.post("/control/models/alpha/load?wait=false", headers=AUTH)
    assert refused.status_code == 409
    assert "unload" in refused.json()["detail"]

    release.set()
    await module.control.background_task


async def test_asking_twice_to_unload_starts_one_unload(
    client, module, gated_unload
) -> None:
    started, release = gated_unload

    await client.post("/control/models/unload?wait=false", headers=AUTH)
    await asyncio.wait_for(started.wait(), timeout=2)
    task = module.control.background_task

    second = await client.post("/control/models/unload?wait=false", headers=AUTH)
    assert second.status_code == 202
    assert second.json()["accepted"] is False
    assert module.control.background_task is task

    release.set()
    await task


async def test_a_detached_unload_failure_still_reaches_the_feed(client, module) -> None:
    async def failing():
        raise RuntimeError("worker would not stop")

    module.control.unload = failing

    response = await client.post("/control/models/unload?wait=false", headers=AUTH)
    assert response.status_code == 202
    await module.control.background_task

    feed = await events_since(client, module)
    failures = [event for event in feed if event["kind"] == "model.failed"]
    assert failures, feed
    assert "worker would not stop" in failures[-1]["summary"]


async def test_both_unload_modes_write_the_same_history(client, module) -> None:
    async def immediate():
        return None

    module.control.unload = immediate

    await client.post("/control/models/unload", headers=AUTH)
    waited = await events_since(client, module)
    cursor = waited[-1]["id"]

    await client.post("/control/models/unload?wait=false", headers=AUTH)
    await module.control.background_task
    detached = await events_since(client, module, cursor)

    def shape(events: list[dict]) -> list[tuple[str, str, str | None]]:
        return [(event["kind"], event["summary"], event["model"]) for event in events]

    assert shape(detached) == shape(waited[-2:])
