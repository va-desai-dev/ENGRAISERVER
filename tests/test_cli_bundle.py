from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from engrai_server.engines.worker import native_worker_environment
from engrai_server.runtime import IMAGE_RUNTIME, MANIFEST_NAME, POLICIES, inspect_runtime, install_runtime
from test_runtime import make_bundle

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("build_cli_bundle", ROOT / "scripts/build-cli-bundle.py")
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def test_packaged_web_files_match_current_source(tmp_path, monkeypatch):
    source = tmp_path / "checkout/src/engrai_server"
    bundle = tmp_path / "bundle"
    for name in ("static/assets/new.js", "static/index.html", "cardgen/web/src/new.js"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("current source")
    monkeypatch.setattr(builder, "ROOT", tmp_path / "checkout")
    with pytest.raises(RuntimeError, match="missing or stale"):
        builder.verify_web_assets(bundle)
    destination = bundle / "_internal/engrai_server"
    shutil.copytree(source, destination)
    builder.verify_web_assets(bundle)
    (destination / "cardgen/web/src/new.js").write_text("previous release")
    with pytest.raises(RuntimeError, match="cardgen/web/src/new.js"):
        builder.verify_web_assets(bundle)


def engine_bundle(root: Path, *, image: bool = False) -> Path:
    bundle = make_bundle(root)
    manifest_path = bundle / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["build"]["options"].append("-DGGML_NATIVE=OFF")
    if image:
        policy = POLICIES[IMAGE_RUNTIME]
        manifest.update(runtime_id=IMAGE_RUNTIME, kernel=policy.kernel)
        manifest["build"]["options"] = [
            *policy.required_options,
            *(f"-D{name}=OFF" for name in policy.backend_options.values()),
            "-DGGML_NATIVE=OFF",
        ]
    manifest_path.write_text(json.dumps(manifest))
    return bundle


def test_packaging_requires_both_matching_portable_engines(tmp_path):
    text = engine_bundle(tmp_path / "text")
    image = engine_bundle(tmp_path / "image", image=True)
    destination = tmp_path / "complete"
    assert builder.embed_runtimes(destination, text, image) == "cpu"
    assert inspect_runtime(destination)["verified"]
    assert inspect_runtime(destination, IMAGE_RUNTIME)["verified"]
    with pytest.raises(ValueError, match="text runtime and an image runtime"):
        builder.embed_runtimes(tmp_path / "wrong", image, text)
    manifest_path = image / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["build"]["options"].remove("-DGGML_NATIVE=OFF")
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="GGML_NATIVE=OFF"):
        builder.embed_runtimes(tmp_path / "native", text, image)


def test_frozen_app_finds_embedded_engines_after_relocation(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    (original / "engrai-server").touch()
    install_runtime(engine_bundle(tmp_path / "source"), original / "runtimes")
    relocated = tmp_path / "moved app"
    original.rename(relocated)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(relocated / "engrai-server"))
    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path / "new-user"))
    status = inspect_runtime(tmp_path / "new-user/runtimes")
    assert status["verified"]
    assert Path(status["executable"]).is_relative_to(relocated)
    assert not (tmp_path / "new-user").exists()  # no copying or mutable app state
    assert not inspect_runtime(tmp_path / "custom-runtimes")["present"]


def test_user_runtime_takes_precedence_and_corruption_is_not_hidden(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    (app / "engrai-server").touch()
    source = engine_bundle(tmp_path / "source")
    install_runtime(source, app / "runtimes")
    root = tmp_path / "user/runtimes"
    installed = install_runtime(source, root)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(app / "engrai-server"))
    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path / "user"))
    assert inspect_runtime(root)["executable"] == installed["executable"]
    Path(installed["executable"]).write_text("corrupted")
    assert inspect_runtime(root)["error"]
    (root / "engrai-text/active.json").write_text("invalid JSON")
    assert not inspect_runtime(root)["verified"]


def test_native_workers_do_not_inherit_frozen_loader_libraries(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/app/_internal")
    monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/host/driver")
    assert native_worker_environment()["LD_LIBRARY_PATH"] == "/host/driver"
    monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
    assert "LD_LIBRARY_PATH" not in native_worker_environment()
    assert native_worker_environment({"LD_LIBRARY_PATH": "/explicit"})["LD_LIBRARY_PATH"] == "/explicit"


def test_frozen_service_uses_stable_launcher(tmp_path, monkeypatch):
    from engrai_server.host import resolve_cli_executable

    stable = tmp_path / "bin/engrai-server"
    stable.parent.mkdir()
    stable.write_text("#!/bin/sh\n")
    stable.chmod(0o755)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("ENGRAI_CLI_LAUNCHER", str(stable))
    monkeypatch.setattr(sys, "argv", [str(tmp_path / "versioned/engrai-server")])
    assert resolve_cli_executable() == stable
