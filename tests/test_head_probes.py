"""HEAD support on the routes an uptime monitor can reach.

FastAPI's @app.get registers GET alone, so every hand-written route answered
405 to a HEAD probe while Starlette's StaticFiles mount answered it fine. A
monitor pointed at / or /healthz therefore reported the gateway down while it
was serving normally.

A HEAD response must carry the same status and headers as GET and no body, so
these assert the body too — a 200 that still ships the whole bundle defeats
the point of probing with HEAD.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# Every GET route that needs no credentials. Authenticated routes are out of
# scope on purpose: a monitor cannot probe them, and widening the method list
# on a route only widens what an unauthenticated caller can reach.
PUBLIC_ROUTES = [
    "/",
    "/display",
    "/healthz",
    "/favicon.svg",
    "/manifest.webmanifest",
    "/control/status",
]


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_API_KEYS", "probe-key-000000")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(tmp_path / "commands"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text-worker.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    # Nothing listens here, so the engine probe fails fast rather than
    # reaching a real runtime on the developer's machine.
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5999")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import importlib

    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    with TestClient(module.app) as instance:
        yield instance
    get_settings.cache_clear()


@pytest.mark.parametrize("route", PUBLIC_ROUTES)
def test_head_is_accepted(client: TestClient, route: str) -> None:
    assert client.head(route).status_code == 200


@pytest.mark.parametrize("route", PUBLIC_ROUTES)
def test_head_returns_no_body(client: TestClient, route: str) -> None:
    assert client.head(route).content == b""


@pytest.mark.parametrize("route", PUBLIC_ROUTES)
def test_head_agrees_with_get(client: TestClient, route: str) -> None:
    """Same status and content type, so a probe sees what a browser sees."""
    head = client.head(route)
    get = client.get(route)
    assert head.status_code == get.status_code
    assert head.headers.get("content-type") == get.headers.get("content-type")


def test_head_does_not_open_an_authenticated_route(client: TestClient) -> None:
    """Widening the method list must not widen what an anonymous caller reads.

    /control/state is admin-only; a HEAD probe must be refused exactly as a
    GET is, never answered with a bare 200.
    """
    assert client.head("/control/state").status_code in {401, 405}
    assert client.get("/control/state").status_code == 401
