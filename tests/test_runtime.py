from __future__ import annotations

import hashlib
import io
import json
import platform
import tarfile
from pathlib import Path

import pytest

from engrai_server.runtime import (
    ACTIVE_NAME,
    POLICIES,
    MANIFEST_NAME,
    RuntimeManagerError,
    host_target,
    inspect_runtime,
    install_runtime,
    runtime_executable,
    verify_bundle,
)


def make_bundle(root: Path, *, backend: str = "cpu") -> Path:
    bundle = root / "bundle"
    worker = bundle / "bin" / "engrai-text-worker"
    worker.parent.mkdir(parents=True)
    worker.write_text("#!/bin/sh\necho engrai\n", encoding="utf-8")
    worker.chmod(0o755)
    operating_system, architecture = host_target()
    payload = worker.read_bytes()
    manifest = {
        "schema_version": 1,
        "runtime_id": "engrai-text",
        "runtime_version": "0.1.0-test",
        "worker_abi": 1,
        "entrypoint": "bin/engrai-text-worker",
        "os": operating_system,
        "arch": architecture,
        "backend": backend,
        "kernel": {
            "name": "llama.cpp",
            "repository": "https://github.com/ggml-org/llama.cpp.git",
            "tag": "v0.5.0",
            "commit": "7fe450e19305b828c199d602c23a8337aaa1f03b",
            "license": "MIT",
        },
        "build": {
            "options": [
                "-DBUILD_SHARED_LIBS=OFF",
                "-DLLAMA_BUILD_UI=OFF",
                "-DLLAMA_USE_PREBUILT_UI=OFF",
                "-DLLAMA_OPENSSL=OFF",
                "-DGGML_STATIC=ON",
                "-DGGML_CUDA=OFF",
                "-DGGML_HIP=OFF",
                "-DGGML_VULKAN=OFF",
                "-DGGML_METAL=OFF",
            ]
        },
        "files": [
            {
                "path": "bin/engrai-text-worker",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "mode": 0o755,
            }
        ],
    }
    (bundle / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return bundle


def test_bundle_verification_records_kernel_and_entrypoint(tmp_path: Path) -> None:
    manifest, executable = verify_bundle(make_bundle(tmp_path))
    assert manifest.runtime_id == "engrai-text"
    assert manifest.kernel["name"] == "llama.cpp"
    assert executable.name == "engrai-text-worker"


def test_bundle_verification_is_read_only_and_rejects_mode_drift(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    executable = bundle / "bin" / "engrai-text-worker"
    executable.chmod(0o644)

    with pytest.raises(RuntimeManagerError, match="mode mismatch"):
        verify_bundle(bundle)
    assert executable.stat().st_mode & 0o777 == 0o644

    result = install_runtime(bundle, tmp_path / "runtimes")
    assert result["verified"] is True


def test_install_uses_versioned_xdg_tree_and_active_record(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    runtime_root = tmp_path / "xdg" / "runtimes"
    result = install_runtime(bundle, runtime_root)

    assert result["result"] == "installed"
    assert result["verified"] is True
    assert result["backend"] == "cpu"
    executable = runtime_executable(runtime_root)
    assert executable == (
        runtime_root
        / "engrai-text"
        / "0.1.0-test"
        / f"{host_target()[0]}-{host_target()[1]}-cpu"
        / "bin"
        / "engrai-text-worker"
    )
    assert json.loads((runtime_root / "engrai-text" / ACTIVE_NAME).read_text())["path"].startswith(
        "engrai-text/0.1.0-test/"
    )


def test_install_is_idempotent_and_detects_later_tampering(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    runtime_root = tmp_path / "runtimes"
    install_runtime(bundle, runtime_root)
    assert install_runtime(bundle, runtime_root)["result"] == "already-current"

    runtime_executable(runtime_root).write_bytes(b"changed")
    status = inspect_runtime(runtime_root)
    assert status["present"] is True
    assert status["verified"] is False
    assert "mismatch" in status["error"]


def test_verification_rejects_undeclared_payload(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    (bundle / "surprise").write_bytes(b"not declared")
    with pytest.raises(RuntimeManagerError, match="undeclared"):
        verify_bundle(bundle)


def test_verification_rejects_a_bundle_for_another_host(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path)
    path = bundle / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["arch"] = "aarch64" if platform.machine().lower() != "aarch64" else "x86_64"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeManagerError, match="this host"):
        verify_bundle(bundle)


def test_verification_rejects_a_manifest_that_lies_about_its_backend(
    tmp_path: Path,
) -> None:
    bundle = make_bundle(tmp_path)
    path = bundle / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["backend"] = "cuda"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeManagerError, match="GGML_CUDA=ON"):
        verify_bundle(bundle)


def test_archive_extraction_rejects_path_traversal(tmp_path: Path) -> None:
    archive = tmp_path / "malicious.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        content = b"escape"
        info = tarfile.TarInfo("../outside")
        info.size = len(content)
        handle.addfile(info, io.BytesIO(content))
    with pytest.raises(RuntimeManagerError, match="normalized relative path"):
        install_runtime(archive, tmp_path / "runtimes")
    assert not (tmp_path / "outside").exists()


def make_image_bundle(root: Path, *, backend: str = "cpu") -> Path:
    bundle = make_bundle(root, backend=backend)
    worker = bundle / "bin" / "engrai-image-worker"
    (bundle / "bin" / "engrai-text-worker").rename(worker)
    path = bundle / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest.update(
        runtime_id="engrai-image",
        runtime_version="0.1.0-sdcpp-test",
        entrypoint="bin/engrai-image-worker",
        kernel=POLICIES["engrai-image"].kernel,
        build={
            "options": [
                *sorted(POLICIES["engrai-image"].required_options),
                *(
                    f"-D{name}={'ON' if name == 'SD_CUDA' and backend == 'cuda' else 'OFF'}"
                    for name in ("SD_CUDA", "SD_HIPBLAS", "SD_VULKAN", "SD_METAL")
                ),
            ]
        },
    )
    manifest["files"][0]["path"] = "bin/engrai-image-worker"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


@pytest.mark.parametrize("engine", ["llama.cpp", "stable-diffusion.cpp"])
def test_installer_policy_matches_the_source_lock(engine: str) -> None:
    lock = json.loads(
        (Path(__file__).parent.parent / "runtime" / f"{engine}.lock.json").read_text()
    )

    assert POLICIES[lock["runtime_id"]].kernel == lock["kernel"]


def test_text_and_image_runtimes_are_active_independently(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtimes"
    install_runtime(make_bundle(tmp_path / "text"), runtime_root)
    install_runtime(make_image_bundle(tmp_path / "image"), runtime_root)

    assert runtime_executable(runtime_root).name == "engrai-text-worker"
    assert runtime_executable(runtime_root, "engrai-image").name == "engrai-image-worker"
    assert inspect_runtime(runtime_root, "engrai-image")["kernel"]["name"] == (
        "stable-diffusion.cpp"
    )


def test_pre_image_installs_keep_their_root_text_record(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtimes"
    install_runtime(make_bundle(tmp_path), runtime_root)
    record = runtime_root / "engrai-text" / ACTIVE_NAME
    record.rename(runtime_root / ACTIVE_NAME)

    assert inspect_runtime(runtime_root)["verified"] is True
    assert inspect_runtime(runtime_root, "engrai-image")["present"] is False


def test_image_bundle_must_disable_the_upstream_web_ui(tmp_path: Path) -> None:
    bundle = make_image_bundle(tmp_path)
    path = bundle / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["build"]["options"].remove("-DSD_SERVER_BUILD_FRONTEND=OFF")
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeManagerError, match="build policy"):
        verify_bundle(bundle)


def test_image_bundle_backend_must_match_its_build(tmp_path: Path) -> None:
    bundle = make_image_bundle(tmp_path)
    path = bundle / MANIFEST_NAME
    manifest = json.loads(path.read_text())
    manifest["backend"] = "cuda"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeManagerError, match="SD_CUDA=ON"):
        verify_bundle(bundle)
