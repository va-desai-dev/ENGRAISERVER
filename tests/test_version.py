"""The build the gateway is currently serving.

Hashed assets are sent `immutable` for a year. That is what makes the
interface fast over a tunnel, and it is also why a client holding a stale
index.html keeps fetching its old bundle successfully — from a CDN edge, even
after the origin has deleted the file. The app looks fine and is simply out of
date, with no symptom to notice. This endpoint is how a client finds out.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTROL_API_KEYS", "eng-version-key")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "a-long-enough-password")
    monkeypatch.setenv("CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("ENGRAI_ROUTE_DIR", str(tmp_path / "commands"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_LOG", str(tmp_path / "text-worker.log"))
    monkeypatch.setenv("MODEL_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("PREFERENCES_PATH", str(tmp_path / "preferences.json"))
    monkeypatch.setenv("ENGRAI_TEXT_ENGINE_PORT", "5999")

    from engrai_server.settings import get_settings

    get_settings.cache_clear()
    import engrai_server.main

    module = importlib.reload(engrai_server.main)
    with TestClient(module.app) as instance:
        instance.headers.clear()
        yield instance
    get_settings.cache_clear()


def test_it_reports_the_shipped_assets(client: TestClient) -> None:
    body = client.get("/control/version").json()
    assets = body["assets"]
    assert any(name.endswith(".js") for name in assets)
    assert any(name.endswith(".css") for name in assets)


def test_it_matches_what_the_shell_actually_references(client: TestClient) -> None:
    """The whole mechanism rests on these two agreeing."""
    shell = client.get("/").text
    for name in client.get("/control/version").json()["assets"]:
        assert f"/assets/{name}" in shell


def test_a_css_only_change_is_still_a_new_version(client: TestClient) -> None:
    """Reporting only the script would miss a stylesheet-only deploy."""
    assets = client.get("/control/version").json()["assets"]
    assert len([name for name in assets if name.endswith(".css")]) >= 1


def test_it_needs_no_credentials(client: TestClient) -> None:
    """The lock screen has to check it, and it discloses nothing new.

    The filenames are already written into the HTML served to any visitor.
    """
    assert client.get("/control/version").status_code == 200


def test_it_answers_head(client: TestClient) -> None:
    assert client.head("/control/version").status_code == 200


def test_it_is_read_from_disk_each_time(client: TestClient, monkeypatch) -> None:
    """A rebuild must be visible without restarting the gateway.

    scripts/build-ui.sh rewrites src/engrai_server/static in place while the service keeps
    running; caching this at import would mean the fix for a stale client is
    itself only delivered by a restart.
    """
    import engrai_server.main as module

    shell = module.static_dir / "index.html"
    original = shell.read_text(encoding="utf-8")
    try:
        shell.write_text(
            '<script type="module" src="/assets/index-DEADBEEF.js"></script>'
            '<link rel="stylesheet" href="/assets/index-CAFE.css">',
            encoding="utf-8",
        )
        assert client.get("/control/version").json()["assets"] == [
            "index-CAFE.css",
            "index-DEADBEEF.js",
        ]
    finally:
        shell.write_text(original, encoding="utf-8")


def test_assets_are_sorted_and_deduplicated(client: TestClient) -> None:
    """A client compares these as a joined string, so ordering is the contract."""
    import engrai_server.main as module

    shell = module.static_dir / "index.html"
    original = shell.read_text(encoding="utf-8")
    try:
        shell.write_text(
            '<script src="/assets/b.js"></script>'
            '<script src="/assets/a.js"></script>'
            '<script src="/assets/b.js"></script>',
            encoding="utf-8",
        )
        assert client.get("/control/version").json()["assets"] == ["a.js", "b.js"]
    finally:
        shell.write_text(original, encoding="utf-8")
