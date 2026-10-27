#!/usr/bin/env python3
"""Build a standalone CLI with BOTH verified inference runtimes embedded.

Run on the target OS/architecture with ENGRAI, PyInstaller, and Node.js/npm
installed. The UI is rebuilt from source before freezing the application.
No Python, package manager, or compiler is needed by the recipient.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
from importlib.metadata import version
from pathlib import Path

from engrai_server.runtime import IMAGE_RUNTIME, TEXT_RUNTIME, host_target, install_runtime

ROOT = Path(__file__).resolve().parents[1]


def verify_web_assets(bundle: Path) -> None:
    """Require every current console and Card Studio file in the frozen app."""
    for directory in ("static", "cardgen/web"):
        source = ROOT / "src" / "engrai_server" / directory
        for path in source.rglob("*"):
            if not path.is_file() or path.name == ".DS_Store":
                continue
            target = bundle / "_internal" / "engrai_server" / directory / path.relative_to(source)
            if not target.is_file() or target.read_bytes() != path.read_bytes():
                raise RuntimeError(f"packaged web asset missing or stale: {directory}/{path.relative_to(source)}")


def smoke_gateway(executable: Path, work: Path, env: dict[str, str]) -> None:
    """Exercise dynamic imports and packaged UI files in the actual executable."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with (work / "gateway.log").open("w+") as log:
        process = subprocess.Popen(
            [str(executable), "serve", "--host", "127.0.0.1", "--port", str(port)],
            cwd=work, env=env, stdout=log, stderr=log,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    log.seek(0)
                    raise RuntimeError(f"packaged gateway exited:\n{log.read()}")
                try:
                    with urllib.request.urlopen(base + "/control/status", timeout=1) as response:
                        if json.load(response) != {"initialized": False}:
                            raise RuntimeError("packaged gateway did not start with fresh state")
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(0.1)
            else:
                raise RuntimeError("packaged gateway did not start within 30 seconds")
            for page in ("/", "/cardgen/index.html"):
                with urllib.request.urlopen(base + page, timeout=5) as response:
                    html = response.read().decode()
                if "<html" not in html.lower():
                    raise RuntimeError(f"packaged UI missing: {page}")
                for asset in re.findall(r'(?:src|href)="([^"#]+)"', html):
                    if asset.startswith("/assets/"):
                        path = asset
                    elif asset.startswith("src/"):
                        path = "/cardgen/" + asset
                    else:
                        continue
                    with urllib.request.urlopen(base + path, timeout=5) as response:
                        if response.status != 200:
                            raise RuntimeError(f"packaged asset missing: {path}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def embed_runtimes(destination: Path, text: Path, image: Path) -> str:
    statuses = [install_runtime(source, destination) for source in (text, image)]
    if [status["runtime"] for status in statuses] != [TEXT_RUNTIME, IMAGE_RUNTIME]:
        raise ValueError("provide a text runtime and an image runtime, in that order")
    backends = {status["backend"] for status in statuses}
    if len(backends) != 1:
        raise ValueError("text and image runtimes must use the same backend")
    for status in statuses:
        if "-DGGML_NATIVE=OFF" not in status["manifest"]["build"]["options"]:
            raise ValueError("public CLI bundles require portable GGML_NATIVE=OFF runtimes")
    return statuses[0]["backend"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text-runtime", type=Path, required=True)
    parser.add_argument("--image-runtime", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "cli")
    args = parser.parse_args()
    operating_system, arch = host_target()
    if operating_system not in {"linux", "macos"} or arch not in {"x86_64", "aarch64"}:
        parser.error(f"unsupported build host: {operating_system}/{arch}")
    subprocess.run([sys.executable, str(ROOT / "scripts/check-version.py")], check=True, cwd=ROOT)
    app_version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if version("engrai-server") != app_version:
        parser.error("installed ENGRAI version differs from this checkout; run uv sync --frozen first")
    subprocess.run(["bash", str(ROOT / "scripts/build-ui.sh")], check=True, cwd=ROOT)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="engrai-cli-build-") as temp:
        work = Path(temp)
        runtime_store = work / "runtimes"
        backend = embed_runtimes(runtime_store, args.text_runtime, args.image_runtime)
        subprocess.run(
            [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
             "--name", "engrai-server", "--distpath", str(work / "dist"),
             "--workpath", str(work / "work"), "--specpath", str(work),
             "--collect-all", "engrai_server", "--collect-all", "uvicorn",
             "--add-data", f"{ROOT / '@models' / 'schemas'}:engrai_server/model_catalog/schemas",
             "--add-data", f"{ROOT / '@models' / 'catalog'}:engrai_server/model_catalog/catalog",
             "--add-data", f"{ROOT / '@models' / 'README.md'}:engrai_server/model_catalog",
             "--recursive-copy-metadata", "engrai-server",
             str(ROOT / "scripts" / "cli-entrypoint.py")],
            check=True, cwd=ROOT,
        )
        bundle = work / "dist" / "engrai-server"
        verify_web_assets(bundle)
        # Data collection sees editable checkout files as well as tracked data.
        # Finder metadata must never enter a downloadable distribution.
        for metadata_file in bundle.rglob(".DS_Store"):
            metadata_file.unlink()
        shutil.move(str(runtime_store), bundle / "runtimes")
        shutil.copy2(ROOT / "LICENSE.md", bundle / "LICENSE.md")
        shutil.copy2(ROOT / "docs" / "ACKNOWLEDGEMENTS.md", bundle / "ACKNOWLEDGEMENTS.md")
        python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
        if not python_license.is_file():
            raise RuntimeError(f"Python license missing from build environment: {python_license}")
        shutil.copy2(python_license, bundle / "PYTHON-LICENSE.txt")
        metadata = {
            "schema_version": 1, "version": app_version,
            "os": operating_system, "arch": arch, "backend": backend,
            "runtimes": [TEXT_RUNTIME, IMAGE_RUNTIME],
        }
        (bundle / "engrai-bundle.json").write_text(json.dumps(metadata, indent=2) + "\n")
        # Test after relocation, with no developer settings or Python environment.
        relocated = work / "relocated bundle"
        bundle.rename(relocated)
        env = {"HOME": str(work / "empty-home"), "ENGRAI_HOME": str(work / "empty-home"),
               "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        executable = relocated / "engrai-server"
        subprocess.run([str(executable), "--version"], check=True, env=env, cwd=work)
        for runtime_id in ("text", "image"):
            result = subprocess.run(
                [str(executable), "runtime", "status", "--runtime", runtime_id, "--json"],
                check=True, capture_output=True, text=True, env=env, cwd=work,
            )
            status = json.loads(result.stdout)
            if not status["verified"] or status["backend"] != backend:
                raise RuntimeError(f"embedded runtime failed verification: {status}")
            subprocess.run([status["executable"], "--help"], check=True,
                           stdout=subprocess.DEVNULL, env=env, cwd=work, timeout=60)
        smoke_gateway(executable, work, env)
        name = f"engrai-server-{operating_system}-{arch}-{backend}.tar.gz"
        archive = args.output_dir.resolve() / name
        with tarfile.open(archive, "w:gz", dereference=True) as handle:
            handle.add(relocated, arcname="engrai-server")
        with archive.open("rb") as handle:
            digest = hashlib.file_digest(handle, "sha256").hexdigest()
        archive.with_name(name + ".sha256").write_text(f"{digest}  {name}\n")
        print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
