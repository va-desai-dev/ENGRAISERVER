#!/usr/bin/env python3
"""Build a pinned engine checkout into a verified ENGRAI runtime bundle.

``--engine llama.cpp`` produces the text runtime (``engrai-text``);
``--engine stable-diffusion.cpp`` produces the image runtime
(``engrai-image``). Sources come from scripts/fetch-engine-sources.py.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
LLAMA_OWNED_OPTIONS = frozenset(
    {
        "BUILD_SHARED_LIBS",
        "CMAKE_BUILD_TYPE",
        "LLAMA_BUILD_APP",
        "LLAMA_BUILD_COMMON",
        "LLAMA_BUILD_EXAMPLES",
        "LLAMA_BUILD_SERVER",
        "LLAMA_BUILD_TESTS",
        "LLAMA_BUILD_TOOLS",
        "LLAMA_BUILD_UI",
        "LLAMA_OPENSSL",
        "LLAMA_USE_PREBUILT_UI",
        "GGML_CUDA",
        "GGML_HIP",
        "GGML_METAL",
        "GGML_METAL_EMBED_LIBRARY",
        "GGML_NATIVE",
        "GGML_STATIC",
        "GGML_VULKAN",
    }
)
SDCPP_OWNED_OPTIONS = frozenset(
    {
        "CMAKE_BUILD_TYPE",
        "SD_BUILD_EXAMPLES",
        "SD_BUILD_SHARED_LIBS",
        "SD_BUILD_SHARED_GGML_LIB",
        "SD_USE_SYSTEM_GGML",
        "SD_USE_UPSTREAM_GGML",
        "SD_SERVER_BUILD_FRONTEND",
        "SD_WEBP",
        "SD_USE_SYSTEM_WEBP",
        "SD_WEBM",
        "SD_CUDA",
        "SD_HIPBLAS",
        "SD_METAL",
        "SD_VULKAN",
        "GGML_NATIVE",
        "GGML_STATIC",
    }
)


def run(*arguments: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(arguments, cwd=cwd, text=True, capture_output=True)
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"command failed ({' '.join(arguments)}):\n{detail}")
    return completed.stdout.strip()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            value.update(chunk)
    return value.hexdigest()


def normalized_target() -> tuple[str, str]:
    system = platform.system().lower()
    operating_system = {"darwin": "macos", "linux": "linux"}.get(system)
    if not operating_system:
        raise SystemExit(f"unsupported build host: {platform.system()}")
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if architecture not in {"x86_64", "aarch64"}:
        raise SystemExit(f"unsupported build architecture: {machine}")
    return operating_system, architecture


def check_extra(extra: list[str], owned: frozenset[str]) -> None:
    for option in extra:
        name = option.removeprefix("-D").split("=", 1)[0]
        if not option.startswith("-D") or name in owned:
            raise SystemExit(f"CMake option is owned by ENGRAI runtime policy: {option}")


def llama_cmake_options(backend: str, native: bool, extra: list[str]) -> list[str]:
    check_extra(extra, LLAMA_OWNED_OPTIONS)
    options = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DBUILD_SHARED_LIBS=OFF",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DLLAMA_BUILD_COMMON=ON",
        "-DLLAMA_BUILD_TESTS=OFF",
        "-DLLAMA_BUILD_EXAMPLES=OFF",
        "-DLLAMA_BUILD_TOOLS=ON",
        "-DLLAMA_BUILD_SERVER=ON",
        "-DLLAMA_BUILD_APP=OFF",
        "-DLLAMA_BUILD_UI=OFF",
        "-DLLAMA_USE_PREBUILT_UI=OFF",
        "-DLLAMA_OPENSSL=OFF",
        f"-DGGML_NATIVE={'ON' if native else 'OFF'}",
        "-DGGML_STATIC=ON",
        "-DGGML_CUDA=OFF",
        "-DGGML_HIP=OFF",
        "-DGGML_VULKAN=OFF",
        "-DGGML_METAL=OFF",
    ]
    enabled = {
        "cuda": "GGML_CUDA",
        "rocm": "GGML_HIP",
        "vulkan": "GGML_VULKAN",
        "metal": "GGML_METAL",
    }.get(backend)
    if enabled:
        options = [item for item in options if not item.startswith(f"-D{enabled}=")]
        options.append(f"-D{enabled}=ON")
    if backend == "metal":
        options.append("-DGGML_METAL_EMBED_LIBRARY=ON")
    options.extend(extra)
    return options


def sdcpp_cmake_options(backend: str, native: bool, extra: list[str]) -> list[str]:
    check_extra(extra, SDCPP_OWNED_OPTIONS)
    enabled = {
        "cuda": "SD_CUDA",
        "rocm": "SD_HIPBLAS",
        "vulkan": "SD_VULKAN",
        "metal": "SD_METAL",
    }.get(backend)
    options = [
        "-DCMAKE_BUILD_TYPE=Release",
        "-DCMAKE_POSITION_INDEPENDENT_CODE=ON",
        "-DSD_BUILD_EXAMPLES=ON",
        "-DSD_BUILD_SHARED_LIBS=OFF",
        "-DSD_BUILD_SHARED_GGML_LIB=OFF",
        "-DSD_USE_SYSTEM_GGML=OFF",
        "-DSD_USE_UPSTREAM_GGML=OFF",
        # The upstream web UI is never shipped; ENGRAI owns the interface.
        "-DSD_SERVER_BUILD_FRONTEND=OFF",
        "-DSD_WEBP=ON",
        "-DSD_USE_SYSTEM_WEBP=OFF",
        "-DSD_WEBM=OFF",
        f"-DGGML_NATIVE={'ON' if native else 'OFF'}",
        "-DGGML_STATIC=ON",
    ]
    for name in ("SD_CUDA", "SD_HIPBLAS", "SD_VULKAN", "SD_METAL"):
        options.append(f"-D{name}={'ON' if name == enabled else 'OFF'}")
    options.extend(extra)
    return options


@dataclass(frozen=True)
class Engine:
    lock: Path
    target: str
    worker: str
    options: Any
    # Standalone license files in the pinned source -> bundle file name.
    licenses: dict[str, str]
    # Notices kept in runtime/notices/<engine>/ for code whose license text is
    # embedded in a header or absent from the source tree. Each lists
    # (vendored path, text) anchors that must still hold in the pinned source,
    # so a pin bump that changes a license fails the build instead of
    # shipping a stale notice.
    notices: dict[str, tuple[tuple[str, str], ...]]


ENGINES = {
    "llama.cpp": Engine(
        lock=ROOT / "runtime" / "llama.cpp.lock.json",
        target="llama-server",
        worker="engrai-text-worker",
        options=llama_cmake_options,
        licenses={
            "LICENSE": "llama.cpp-MIT.txt",
            "vendor/cpp-httplib/LICENSE": "cpp-httplib-MIT.txt",
            "licenses/LICENSE-jsonhpp": "nlohmann-json-MIT.txt",
            "vendor/hash/rotate-bits/LICENSE.md": "rotate-bits-MIT.txt",
            "vendor/hash/sha256/LICENSE": "sha256-public-domain.txt",
        },
        notices={
            "stb-MIT-or-Unlicense.txt": (
                ("vendor/stb/stb_image.h", "Copyright (c) 2017 Sean Barrett"),
            ),
            "miniaudio-MIT-0-or-Unlicense.txt": (
                ("vendor/miniaudio/miniaudio.h", "Copyright 2026 David Reid"),
            ),
            "subprocess-Unlicense.txt": (
                (
                    "vendor/sheredom/subprocess.h",
                    "This is free and unencumbered software released into the public domain.",
                ),
            ),
            "xxhash-BSD-2-Clause.txt": (
                ("vendor/hash/xxhash/xxhash.h", "Copyright (C) 2012-2023 Yann Collet"),
            ),
            "sha1-public-domain.txt": (("vendor/hash/sha1/sha1.h", "100% Public Domain"),),
        },
    ),
    "stable-diffusion.cpp": Engine(
        lock=ROOT / "runtime" / "stable-diffusion.cpp.lock.json",
        target="sd-server",
        worker="engrai-image-worker",
        options=sdcpp_cmake_options,
        licenses={
            "LICENSE": "stable-diffusion.cpp-MIT.txt",
            "ggml/LICENSE": "ggml-MIT.txt",
            "thirdparty/libwebp/COPYING": "libwebp-BSD-3-Clause.txt",
            "thirdparty/LICENSE.darts_clone.txt": "darts-clone-BSD-2-Clause.txt",
            "thirdparty/oniguruma/COPYING": "oniguruma-BSD-2-Clause.txt",
            "thirdparty/utf8proc/LICENSE.md": "utf8proc-MIT.txt",
        },
        notices={
            "cpp-httplib-MIT.txt": (("thirdparty/httplib.h", "Copyright (c) 2025 Yuji Hirose"),),
            "nlohmann-json-MIT.txt": (
                ("thirdparty/json.hpp", "SPDX-FileCopyrightText: 2013-2022 Niels Lohmann"),
            ),
            "kuba-zip-MIT.txt": (("thirdparty/zip.h", "#define ZIP_H"),),
            "miniz-MIT.txt": (
                ("thirdparty/miniz.h", "Copyright 2013-2014 RAD Game Tools and Valve Software"),
            ),
            "stb-MIT-or-Unlicense.txt": (
                ("thirdparty/stb_image.h", "Copyright (c) 2017 Sean Barrett"),
                ("thirdparty/stb_image_write.h", "Copyright (c) 2017 Sean Barrett"),
                ("thirdparty/stb_image_resize.h", "This software is in the public domain."),
            ),
        },
    ),
}


def collect_licenses(name: str, engine: Engine, source: Path, destination: Path) -> list[str]:
    """Copy every license the worker needs into ``destination``.

    Fails when a standalone license file is missing, when a notice's anchors
    no longer match the pinned source, or when runtime/notices/<engine> holds
    a notice that is not registered (and so would silently not ship).
    """

    destination.mkdir(parents=True, exist_ok=True)
    for relative, target in engine.licenses.items():
        path = source / relative
        if not path.is_file():
            raise SystemExit(f"{name} license file is missing from the pinned source: {relative}")
        shutil.copy2(path, destination / target)
    notice_dir = ROOT / "runtime" / "notices" / name
    present = {path.name for path in notice_dir.glob("*.txt")} if notice_dir.is_dir() else set()
    unregistered = present - engine.notices.keys()
    if unregistered:
        raise SystemExit(f"unregistered {name} notices: {', '.join(sorted(unregistered))}")
    for target, anchors in engine.notices.items():
        notice = notice_dir / target
        if not notice.is_file():
            raise SystemExit(f"{name} notice is missing: runtime/notices/{name}/{target}")
        for relative, anchor in anchors:
            path = source / relative
            text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
            if anchor not in text:
                raise SystemExit(
                    f"{name} notice {target} is stale: {relative} no longer contains {anchor!r}"
                )
        shutil.copy2(notice, destination / target)
    return sorted([*engine.licenses.values(), *engine.notices])


def source_revision(source: Path) -> str:
    try:
        return run("git", "rev-parse", "HEAD", cwd=source)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise SystemExit(f"engine source must be a Git checkout: {source}: {exc}") from exc


def file_record(path: Path, root: Path) -> dict[str, Any]:
    mode = stat.S_IMODE(path.stat().st_mode)
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": digest(path),
        "size": path.stat().st_size,
        "mode": mode,
    }


def write_archive(bundle: Path, destination: Path, epoch: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for path in sorted(bundle.rglob("*"), key=lambda value: value.as_posix()):
                    relative = path.relative_to(bundle).as_posix()
                    info = archive.gettarinfo(str(path), arcname=relative)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = epoch
                    if path.is_file():
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--engine",
        choices=sorted(ENGINES),
        default="llama.cpp",
        help="engine to build (default: llama.cpp)",
    )
    parser.add_argument(
        "--source",
        type=Path,
        help="pinned engine checkout (default: third_party/<engine>)",
    )
    parser.add_argument(
        "--backend",
        choices=("cpu", "cuda", "rocm", "vulkan", "metal"),
        required=True,
    )
    parser.add_argument("--build-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist" / "runtime")
    parser.add_argument("--native", action="store_true", help="optimize for only this build host")
    parser.add_argument(
        "--cmake-option",
        action="append",
        default=[],
        help="additional recorded -D option (repeatable)",
    )
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    args = parser.parse_args()

    engine = ENGINES[args.engine]
    lock = json.loads(engine.lock.read_text(encoding="utf-8"))
    runtime_id = lock["runtime_id"]
    source = (args.source or ROOT / "third_party" / args.engine).expanduser().resolve()
    expected = lock["kernel"]["commit"]
    observed = source_revision(source)
    if observed != expected:
        raise SystemExit(
            f"refusing unpinned {args.engine} source: expected {expected}, observed {observed}"
        )
    for path, commit in lock.get("submodules", {}).items():
        found = source_revision(source / path)
        if found != commit:
            raise SystemExit(
                f"refusing unpinned {args.engine} submodule {path}: "
                f"expected {commit}, observed {found}"
            )
    if not (source / "CMakeLists.txt").is_file():
        raise SystemExit(f"incomplete {args.engine} source checkout: {source}")
    # Checked before the long compile so a stale notice fails fast.
    with tempfile.TemporaryDirectory(prefix="engrai-licenses-") as check:
        collect_licenses(args.engine, engine, source, Path(check))

    operating_system, architecture = normalized_target()
    if args.backend == "metal" and operating_system != "macos":
        raise SystemExit("the Metal bundle must be built on macOS")
    build_dir = (
        args.build_dir.expanduser().resolve()
        if args.build_dir
        else ROOT / "build" / f"{runtime_id}-{operating_system}-{architecture}-{args.backend}"
    )
    options = engine.options(args.backend, args.native, args.cmake_option)
    run("cmake", "-S", str(source), "-B", str(build_dir), *options)
    run(
        "cmake",
        "--build",
        str(build_dir),
        "--config",
        "Release",
        "--target",
        engine.target,
        "-j",
        str(args.jobs),
    )

    candidates = [
        build_dir / "bin" / engine.target,
        build_dir / "bin" / "Release" / engine.target,
    ]
    built = next((candidate for candidate in candidates if candidate.is_file()), None)
    if built is None:
        raise SystemExit(
            f"CMake completed without producing {engine.target} under {build_dir / 'bin'}"
        )

    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", "1700000000"))
    with tempfile.TemporaryDirectory(prefix="engrai-runtime-") as temporary:
        bundle = Path(temporary)
        worker = bundle / "bin" / engine.worker
        worker.parent.mkdir(parents=True)
        shutil.copy2(built, worker)
        worker.chmod(0o755)
        collect_licenses(args.engine, engine, source, bundle / "licenses")
        lock_copy = bundle / "provenance" / engine.lock.name
        lock_copy.parent.mkdir(parents=True)
        shutil.copy2(engine.lock, lock_copy)

        payload_files = [path for path in bundle.rglob("*") if path.is_file()]
        manifest = {
            "schema_version": 1,
            "runtime_id": runtime_id,
            "runtime_version": lock["runtime_version"],
            "worker_abi": 1,
            "entrypoint": f"bin/{engine.worker}",
            "os": operating_system,
            "arch": architecture,
            "backend": args.backend,
            "kernel": lock["kernel"],
            "build": {
                "cmake": run("cmake", "--version").splitlines()[0],
                "options": options,
                "native": args.native,
                "source_date_epoch": epoch,
            },
            "files": [file_record(path, bundle) for path in sorted(payload_files)],
        }
        (bundle / "engrai-runtime.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        name = (
            f"{runtime_id}-{lock['runtime_version']}-{operating_system}-"
            f"{architecture}-{args.backend}.tar.gz"
        )
        destination = args.output_dir.expanduser().resolve() / name
        write_archive(bundle, destination, epoch)
        print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
