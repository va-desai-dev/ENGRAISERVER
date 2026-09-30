from __future__ import annotations

from pathlib import Path

import pytest

from engrai_server.preferences import PreferenceError, PreferenceStore


def test_defaults_to_the_environment_roots(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    assert store.search_roots == [tmp_path]
    assert store.public_dict()["is_default"] is True
    # Reading must not create the file.
    assert not (tmp_path / "prefs.json").exists()


def test_setting_roots_persists(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    store = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    store.set_search_roots(str(library))
    assert store.search_roots == [library]

    reopened = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    assert reopened.search_roots == [library]
    assert reopened.public_dict()["is_default"] is False


def test_blank_restores_the_default(tmp_path: Path) -> None:
    library = tmp_path / "library"
    library.mkdir()
    store = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    store.set_search_roots(str(library))
    store.set_search_roots("")
    assert store.search_roots == [tmp_path]


def test_rejects_a_path_that_is_not_a_directory(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    with pytest.raises(PreferenceError, match="not a directory"):
        store.set_search_roots(str(tmp_path / "nowhere"))


def test_rejects_a_relative_path(tmp_path: Path) -> None:
    store = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    with pytest.raises(PreferenceError, match="not an absolute path"):
        store.set_search_roots("models")


def test_accepts_several_roots(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir(); second.mkdir()
    store = PreferenceStore(tmp_path / "prefs.json", default_roots=[tmp_path])
    store.set_search_roots(f"{first}:{second}")
    assert store.search_roots == [first, second]


def test_required_download_root_cannot_be_hidden_by_preferences(tmp_path: Path) -> None:
    library, downloads = tmp_path / "library", tmp_path / "downloads"
    library.mkdir(); downloads.mkdir()
    store = PreferenceStore(
        tmp_path / "prefs.json",
        default_roots=[tmp_path],
        required_roots=[downloads],
    )
    store.set_search_roots(str(library))
    assert store.search_roots == [library, downloads]
