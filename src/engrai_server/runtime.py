"""Install and inspect ENGRAI-owned inference runtime bundles.

The Python package never searches ``PATH`` for an arbitrary engine and never
downloads an upstream executable. Release engineering builds pinned engine
revisions (llama.cpp for text, stable-diffusion.cpp for images) into ENGRAI
runtime bundles; this module verifies a bundle, installs it under XDG data,
and atomically selects it, independently for each runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import tarfile
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any


MANIFEST_NAME = "engrai-runtime.json"
ACTIVE_NAME = "active.json"
TEXT_RUNTIME = "engrai-text"
IMAGE_RUNTIME = "engrai-image"
RUNTIME_ID = TEXT_RUNTIME
SCHEMA_VERSION = 1
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BACKENDS = frozenset({"cpu", "cuda", "rocm", "vulkan", "metal"})


@dataclass(frozen=True, slots=True)
class RuntimePolicy:
    """What a bundle for one runtime must prove about its source and build.

    ``kernel`` mirrors ``runtime/<engine>.lock.json``; a test keeps them equal.
    """

    label: str
    kernel: dict[str, str]
    required_options: frozenset[str]
    backend_options: dict[str, str]


POLICIES: dict[str, RuntimePolicy] = {
    TEXT_RUNTIME: RuntimePolicy(
        label="text",
        kernel={
            "name": "llama.cpp",
            "repository": "https://github.com/ggml-org/llama.cpp.git",
            "tag": "v0.5.0",
            "commit": "7fe450e19305b828c199d602c23a8337aaa1f03b",
            "license": "MIT",
        },
        required_options=frozenset(
            {
                "-DBUILD_SHARED_LIBS=OFF",
                "-DLLAMA_BUILD_UI=OFF",
                "-DLLAMA_USE_PREBUILT_UI=OFF",
                "-DLLAMA_OPENSSL=OFF",
                "-DGGML_STATIC=ON",
            }
        ),
        backend_options={
            "cuda": "GGML_CUDA",
            "rocm": "GGML_HIP",
            "vulkan": "GGML_VULKAN",
            "metal": "GGML_METAL",
        },
    ),
    IMAGE_RUNTIME: RuntimePolicy(
        label="image",
        kernel={
            "name": "stable-diffusion.cpp",
            "repository": "https://github.com/leejet/stable-diffusion.cpp.git",
            "tag": "master-ac45422",
            "commit": "ac45422a05fd962cfae3c8f358e461cb647b8bd7",
            "license": "MIT",
        },
        required_options=frozenset(
            {
                "-DSD_BUILD_SHARED_LIBS=OFF",
                "-DSD_BUILD_SHARED_GGML_LIB=OFF",
                "-DSD_USE_SYSTEM_GGML=OFF",
                "-DSD_USE_UPSTREAM_GGML=OFF",
                "-DSD_SERVER_BUILD_FRONTEND=OFF",
                "-DGGML_STATIC=ON",
            }
        ),
        backend_options={
            "cuda": "SD_CUDA",
            "rocm": "SD_HIPBLAS",
            "vulkan": "SD_VULKAN",
            "metal": "SD_METAL",
        },
    ),
}


def policy_for(runtime_id: str) -> RuntimePolicy:
    try:
        return POLICIES[runtime_id]
    except KeyError:
        known = ", ".join(sorted(POLICIES))
        raise RuntimeManagerError(
            f"unknown runtime {runtime_id!r}; expected one of: {known}"
        ) from None


class RuntimeManagerError(RuntimeError):
    """An ENGRAI runtime bundle could not be installed or verified."""


@dataclass(frozen=True, slots=True)
class RuntimeFile:
    path: str
    sha256: str
    size: int
    mode: int


@dataclass(frozen=True, slots=True)
class RuntimeManifest:
    schema_version: int
    runtime_id: str
    runtime_version: str
    worker_abi: int
    entrypoint: str
    os: str
    arch: str
    backend: str
    kernel: dict[str, str]
    build: dict[str, Any]
    files: tuple[RuntimeFile, ...]

    @property
    def target(self) -> str:
        return f"{self.os}-{self.arch}-{self.backend}"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RuntimeManifest":
        try:
            files = tuple(RuntimeFile(**item) for item in payload["files"])
            manifest = cls(
                schema_version=payload["schema_version"],
                runtime_id=payload["runtime_id"],
                runtime_version=payload["runtime_version"],
                worker_abi=payload["worker_abi"],
                entrypoint=payload["entrypoint"],
                os=payload["os"],
                arch=payload["arch"],
                backend=payload["backend"],
                kernel=payload["kernel"],
                build=payload["build"],
                files=files,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeManagerError(f"invalid runtime manifest: {exc}") from exc
        _validate_manifest_shape(manifest)
        return manifest


def _safe_relative(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise RuntimeManagerError(f"{label} must be a normalized relative path: {value!r}")
    return path


def _validate_manifest_shape(manifest: RuntimeManifest) -> None:
    if manifest.schema_version != SCHEMA_VERSION:
        raise RuntimeManagerError(
            f"runtime manifest schema {manifest.schema_version} is unsupported; "
            f"expected {SCHEMA_VERSION}"
        )
    policy = policy_for(manifest.runtime_id)
    for label, value in (
        ("runtime version", manifest.runtime_version),
        ("operating system", manifest.os),
        ("architecture", manifest.arch),
        ("backend", manifest.backend),
    ):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise RuntimeManagerError(f"invalid {label}: {value!r}")
    if manifest.worker_abi != 1:
        raise RuntimeManagerError(
            f"worker ABI {manifest.worker_abi} is unsupported; expected 1"
        )
    if manifest.os not in {"linux", "macos"}:
        raise RuntimeManagerError(f"unsupported runtime operating system: {manifest.os}")
    if manifest.arch not in {"x86_64", "aarch64"}:
        raise RuntimeManagerError(f"unsupported runtime architecture: {manifest.arch}")
    if manifest.backend not in _BACKENDS:
        raise RuntimeManagerError(f"unsupported runtime backend: {manifest.backend}")
    if not isinstance(manifest.kernel, dict) or manifest.kernel != policy.kernel:
        raise RuntimeManagerError("runtime kernel provenance does not match ENGRAI's source lock")
    if not _COMMIT.fullmatch(manifest.kernel["commit"]):
        raise RuntimeManagerError("runtime kernel commit must be a full Git object ID")
    if not isinstance(manifest.build, dict):
        raise RuntimeManagerError("runtime build provenance must be an object")
    options = manifest.build.get("options")
    if not isinstance(options, list) or not all(isinstance(item, str) for item in options):
        raise RuntimeManagerError("runtime build options must be a string array")
    if not policy.required_options.issubset(options):
        raise RuntimeManagerError("runtime bundle does not satisfy ENGRAI's private-worker build policy")
    for backend, cmake_name in policy.backend_options.items():
        expected = "ON" if manifest.backend == backend else "OFF"
        values = [item.rsplit("=", 1)[-1] for item in options if item.startswith(f"-D{cmake_name}=")]
        if values != [expected]:
            raise RuntimeManagerError(
                f"runtime backend {manifest.backend} requires exactly -D{cmake_name}={expected}"
            )
    entrypoint = _safe_relative(manifest.entrypoint, label="runtime entrypoint")
    seen: set[str] = set()
    for item in manifest.files:
        path = str(_safe_relative(item.path, label="runtime file"))
        if path == MANIFEST_NAME or path in seen:
            raise RuntimeManagerError(f"duplicate or reserved runtime file: {path}")
        seen.add(path)
        if not _SHA256.fullmatch(item.sha256):
            raise RuntimeManagerError(f"invalid SHA-256 for runtime file {path}")
        if not isinstance(item.size, int) or item.size < 0:
            raise RuntimeManagerError(f"invalid size for runtime file {path}")
        if not isinstance(item.mode, int) or item.mode & ~0o777:
            raise RuntimeManagerError(f"invalid mode for runtime file {path}")
    if str(entrypoint) not in seen:
        raise RuntimeManagerError("runtime entrypoint is not listed in manifest files")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def load_manifest(bundle: Path) -> RuntimeManifest:
    path = bundle / MANIFEST_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeManagerError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeManagerError("runtime manifest must be a JSON object")
    return RuntimeManifest.from_dict(payload)


def host_target() -> tuple[str, str]:
    system = platform.system().lower()
    operating_system = {"darwin": "macos", "linux": "linux"}.get(system, system)
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return operating_system, architecture


def verify_bundle(
    bundle: Path,
    *,
    require_host: bool = True,
) -> tuple[RuntimeManifest, Path]:
    """Verify all bytes in an extracted bundle and return its entrypoint."""

    manifest = load_manifest(bundle)
    if require_host:
        expected_os, expected_arch = host_target()
        if (manifest.os, manifest.arch) != (expected_os, expected_arch):
            raise RuntimeManagerError(
                f"bundle targets {manifest.os}/{manifest.arch}, but this host is "
                f"{expected_os}/{expected_arch}"
            )

    declared = {item.path: item for item in manifest.files}
    observed: set[str] = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise RuntimeManagerError(f"runtime bundles cannot contain symlinks: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(bundle).as_posix()
        if relative == MANIFEST_NAME:
            continue
        observed.add(relative)
    undeclared = observed - declared.keys()
    missing = declared.keys() - observed
    if undeclared or missing:
        detail = []
        if undeclared:
            detail.append("undeclared: " + ", ".join(sorted(undeclared)))
        if missing:
            detail.append("missing: " + ", ".join(sorted(missing)))
        raise RuntimeManagerError(
            "runtime payload does not match manifest (" + "; ".join(detail) + ")"
        )

    for relative, item in declared.items():
        path = bundle / relative
        if path.stat().st_size != item.size:
            raise RuntimeManagerError(f"size mismatch for runtime file {relative}")
        if sha256_file(path) != item.sha256:
            raise RuntimeManagerError(f"checksum mismatch for runtime file {relative}")
        observed_mode = path.stat().st_mode & 0o777
        if observed_mode != item.mode:
            raise RuntimeManagerError(
                f"mode mismatch for runtime file {relative}: "
                f"expected {item.mode:#05o}, found {observed_mode:#05o}"
            )

    entrypoint = bundle / manifest.entrypoint
    if not entrypoint.is_file() or not os.access(entrypoint, os.X_OK):
        raise RuntimeManagerError(f"runtime entrypoint is not executable: {entrypoint}")
    return manifest, entrypoint


def _extract_archive(archive: Path, destination: Path) -> None:
    try:
        handle = tarfile.open(archive, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise RuntimeManagerError(f"could not open runtime bundle {archive}: {exc}") from exc
    with handle:
        for member in handle.getmembers():
            relative = _safe_relative(member.name.rstrip("/"), label="archive member")
            target = destination.joinpath(*relative.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                raise RuntimeManagerError(
                    f"runtime archives may contain only files and directories: {member.name}"
                )
            source = handle.extractfile(member)
            if source is None:
                raise RuntimeManagerError(f"could not read archive member {member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def _copy_directory(source: Path, destination: Path) -> None:
    for path in source.rglob("*"):
        if path.is_symlink():
            raise RuntimeManagerError(f"runtime bundles cannot contain symlinks: {path}")
    shutil.copytree(source, destination, dirs_exist_ok=True)


def _apply_manifest_modes(bundle: Path) -> None:
    """Apply declared modes only to a private installation staging tree."""

    manifest = load_manifest(bundle)
    for item in manifest.files:
        path = bundle.joinpath(*PurePosixPath(item.path).parts)
        if path.is_file() and not path.is_symlink():
            path.chmod(item.mode)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def install_runtime(
    source: Path,
    runtime_root: Path,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Verify, install, and atomically activate an ENGRAI runtime bundle."""

    source = source.expanduser().resolve()
    if not source.exists():
        raise RuntimeManagerError(f"runtime bundle not found: {source}")
    runtime_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".runtime-install-", dir=runtime_root))
    payload = temporary / "payload"
    try:
        payload.mkdir()
        if source.is_dir():
            _copy_directory(source, payload)
        elif source.is_file():
            _extract_archive(source, payload)
        else:
            raise RuntimeManagerError(f"runtime bundle is not a file or directory: {source}")

        _apply_manifest_modes(payload)
        manifest, _ = verify_bundle(payload)
        destination = (
            runtime_root
            / manifest.runtime_id
            / manifest.runtime_version
            / manifest.target
        )
        result = "installed"
        if destination.exists():
            try:
                current, _ = verify_bundle(destination)
            except RuntimeManagerError:
                current = None
            if current == manifest:
                result = "already-current"
            elif not force:
                raise RuntimeManagerError(
                    f"runtime destination already exists: {destination}; use --force to replace it"
                )
            else:
                backup = destination.with_name(f".{destination.name}.replaced")
                if backup.exists():
                    shutil.rmtree(backup)
                os.replace(destination, backup)
                try:
                    os.replace(payload, destination)
                except BaseException:
                    os.replace(backup, destination)
                    raise
                shutil.rmtree(backup)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(payload, destination)

        _atomic_json(
            _active_path(runtime_root, manifest.runtime_id),
            {
                "schema_version": SCHEMA_VERSION,
                "runtime_id": manifest.runtime_id,
                "runtime_version": manifest.runtime_version,
                "target": manifest.target,
                "path": str(destination.relative_to(runtime_root)),
            },
        )
        return {**inspect_runtime(runtime_root, manifest.runtime_id), "result": result}
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def _active_path(runtime_root: Path, runtime_id: str) -> Path:
    return runtime_root / runtime_id / ACTIVE_NAME


def _active_directory(runtime_root: Path, runtime_id: str) -> Path | None:
    candidates = [_active_path(runtime_root, runtime_id)]
    if runtime_id == TEXT_RUNTIME:
        # Installs made before image runtimes kept one record at the root.
        candidates.append(runtime_root / ACTIVE_NAME)
    for record in candidates:
        try:
            payload = json.loads(record.read_text(encoding="utf-8"))
            if payload.get("runtime_id", runtime_id) != runtime_id:
                continue
            relative = _safe_relative(payload["path"], label="active runtime path")
            break
        except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError, RuntimeManagerError):
            continue
    else:
        return None
    candidate = runtime_root.joinpath(*relative.parts).resolve()
    root = runtime_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def inspect_runtime(
    runtime_root: Path,
    runtime_id: str = TEXT_RUNTIME,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    policy_for(runtime_id)
    directory = _active_directory(runtime_root, runtime_id)
    base: dict[str, Any] = {
        "runtime": runtime_id,
        "root": str(runtime_root),
        "path": str(directory) if directory else None,
        "present": bool(directory and directory.is_dir()),
        "verified": False,
        "version": None,
        "backend": None,
        "target": None,
        "worker_abi": None,
        "kernel": None,
        "executable": None,
        "error": None,
    }
    if not directory or not directory.is_dir():
        return base
    try:
        if verify:
            manifest, executable = verify_bundle(directory)
        else:
            manifest = load_manifest(directory)
            executable = directory / manifest.entrypoint
        if manifest.runtime_id != runtime_id:
            raise RuntimeManagerError(
                f"active {runtime_id} record points at a {manifest.runtime_id} bundle"
            )
        base.update(
            {
                "verified": verify,
                "version": manifest.runtime_version,
                "backend": manifest.backend,
                "target": manifest.target,
                "worker_abi": manifest.worker_abi,
                "kernel": manifest.kernel,
                "executable": str(executable),
                "manifest": asdict(manifest),
            }
        )
    except RuntimeManagerError as exc:
        base["error"] = str(exc)
    return base


def runtime_executable(runtime_root: Path, runtime_id: str = TEXT_RUNTIME) -> Path:
    label = policy_for(runtime_id).label
    status = inspect_runtime(runtime_root, runtime_id)
    if not status["present"]:
        raise RuntimeManagerError(
            f"no ENGRAI {label} runtime is active; install one with "
            "`engrai-server runtime install <bundle>`"
        )
    if not status["verified"]:
        raise RuntimeManagerError(
            f"active ENGRAI {label} runtime failed verification: {status['error']}"
        )
    return Path(status["executable"])


def print_status(status: dict[str, Any]) -> None:
    label = POLICIES[status["runtime"]].label if status.get("runtime") in POLICIES else "text"
    print(f"ENGRAI {label} runtime: {status['root']}")
    print(f"  active:     {'yes' if status['present'] else 'no'}")
    print(f"  verified:   {'yes' if status['verified'] else 'no'}")
    print(f"  version:    {status['version'] or 'none'}")
    print(f"  target:     {status['target'] or 'none'}")
    print(f"  executable: {status['executable'] or 'none'}")
    if kernel := status.get("kernel"):
        print(f"  kernel:     {kernel.get('name')} {kernel.get('tag')} ({kernel.get('commit')})")
    if error := status.get("error"):
        print(f"  error:      {error}")
