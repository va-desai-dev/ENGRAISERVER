from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_store(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep tests from reading the host's installed ENGRAI runtimes.

    The default runtime store lives under XDG data, so without this the image
    and text engines a test exercises would depend on what happens to be
    installed on the machine running it. Tests that set ENGRAI_HOME or their
    own XDG paths still take precedence.
    """

    data = tmp_path_factory.mktemp("xdg-data")
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.delenv("ENGRAI_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("RUNTIME_DIR", raising=False)
    return data
