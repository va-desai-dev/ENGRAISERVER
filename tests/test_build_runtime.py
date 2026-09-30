from __future__ import annotations

import dataclasses
import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build_runtime", ROOT / "scripts" / "build-runtime.py")
assert _spec and _spec.loader
build_runtime = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = build_runtime  # dataclasses resolve their module by name
_spec.loader.exec_module(build_runtime)


@pytest.mark.parametrize("name", sorted(build_runtime.ENGINES))
def test_every_registered_notice_exists_and_says_something(name: str) -> None:
    engine = build_runtime.ENGINES[name]
    notice_dir = ROOT / "runtime" / "notices" / name

    assert {path.name for path in notice_dir.glob("*.txt")} == set(engine.notices)
    for target in engine.notices:
        text = (notice_dir / target).read_text(encoding="utf-8")
        assert "Upstream:" in text and "Vendored as:" in text
        assert len(text.splitlines()) > 5


@pytest.mark.parametrize("name", sorted(build_runtime.ENGINES))
def test_pinned_source_yields_the_complete_license_set(name: str, tmp_path: Path) -> None:
    source = ROOT / "third_party" / name
    if not (source / "CMakeLists.txt").is_file():
        pytest.skip(f"{name} source not fetched (scripts/fetch-engine-sources.py)")
    engine = build_runtime.ENGINES[name]

    shipped = build_runtime.collect_licenses(name, engine, source, tmp_path)

    assert shipped == sorted({*engine.licenses.values(), *engine.notices})
    assert sorted(path.name for path in tmp_path.iterdir()) == shipped


def fake_source(tmp_path: Path, name: str) -> Path:
    """A source tree holding exactly the files and anchors the engine expects."""

    engine = build_runtime.ENGINES[name]
    source = tmp_path / "source"
    for relative in engine.licenses:
        (source / relative).parent.mkdir(parents=True, exist_ok=True)
        (source / relative).write_text("license\n")
    for anchors in engine.notices.values():
        for relative, anchor in anchors:
            path = source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            existing = path.read_text() if path.exists() else ""
            path.write_text(existing + anchor + "\n")
    return source


def test_a_changed_license_anchor_fails_the_build(tmp_path: Path) -> None:
    name = "stable-diffusion.cpp"
    source = fake_source(tmp_path, name)
    (source / "thirdparty" / "httplib.h").write_text("// Copyright (c) 2031 Someone Else\n")

    with pytest.raises(SystemExit, match="cpp-httplib-MIT.txt is stale"):
        build_runtime.collect_licenses(name, build_runtime.ENGINES[name], source, tmp_path / "out")


def test_a_missing_license_file_fails_the_build(tmp_path: Path) -> None:
    name = "llama.cpp"
    source = fake_source(tmp_path, name)
    (source / "vendor" / "cpp-httplib" / "LICENSE").unlink()

    with pytest.raises(SystemExit, match="vendor/cpp-httplib/LICENSE"):
        build_runtime.collect_licenses(name, build_runtime.ENGINES[name], source, tmp_path / "out")


def test_an_unregistered_notice_fails_the_build(tmp_path: Path) -> None:
    name = "stable-diffusion.cpp"
    engine = build_runtime.ENGINES[name]
    source = fake_source(tmp_path, name)
    partial = dataclasses.replace(
        engine, notices={k: v for k, v in engine.notices.items() if k != "miniz-MIT.txt"}
    )

    with pytest.raises(SystemExit, match="unregistered stable-diffusion.cpp notices: miniz-MIT.txt"):
        build_runtime.collect_licenses(name, partial, source, tmp_path / "out")
