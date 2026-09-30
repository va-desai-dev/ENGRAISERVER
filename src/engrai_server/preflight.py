"""Dependency checks for an ENGRAI host.

The repo carries the control plane and the pinned runtime recipe. Compiled
runtime bundles, vendor GPU runtimes, and GGUF weights are installed into
their platform-appropriate stores. This module verifies those boundaries and
prints the exact command that fixes each missing dependency.

Run standalone:  engrai-server preflight
"""

from __future__ import annotations

import ctypes.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from .credentials import CredentialStore
from .profiles import ProfileStore
from .runtime import inspect_runtime
from .settings import Settings, app_home, get_settings


Status = Literal["ok", "warn", "fail"]

# The GGUF path a profile stores lives inside the Hugging Face cache layout:
#   <root>/models--<owner>--<repo>/snapshots/<revision>/<file.gguf>
HF_CACHE_PATH = re.compile(
    r"models--(?P<owner>[^/]+?)--(?P<repo>[^/]+?)/snapshots/(?P<revision>[^/]+)/(?P<filename>.+)$"
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str
    remedy: str = ""


@dataclass(frozen=True, slots=True)
class HFReference:
    repo_id: str
    filename: str
    revision: str


# Large quants are published split across several files, and every part must
# be present for the model to load. The engine is handed part one; the rest
# are found beside it.
SPLIT_PART = re.compile(r"^(?P<stem>.+)-(?P<index>\d+)-of-(?P<total>\d+)\.gguf$", re.I)


def split_siblings(filename: str) -> list[str]:
    """Every part of a split GGUF, or just the file itself when not split."""
    match = SPLIT_PART.match(filename)
    if not match:
        return [filename]
    width = len(match.group("index"))
    total = int(match.group("total"))
    stem = match.group("stem")
    return [
        f"{stem}-{index:0{width}d}-of-{match.group('total')}.gguf"
        for index in range(1, total + 1)
    ]


def parse_hf_reference(model_path: str) -> HFReference | None:
    """Recover the repo, file, and revision from a Hugging Face cache path.

    The revision matters: re-downloading at the current head lands the file
    under a different snapshot directory, so the absolute path saved in the
    profile would no longer resolve. Pinning it reproduces the original path
    byte for byte, and guarantees the same weights rather than a silently
    re-quantised upload.

    Returns None for weights kept outside the HF cache, which cannot be
    re-fetched automatically and must be restored by hand.
    """
    match = HF_CACHE_PATH.search(model_path.replace(os.sep, "/"))
    if not match:
        return None
    return HFReference(
        repo_id=f"{match.group('owner')}/{match.group('repo')}",
        filename=match.group("filename"),
        revision=match.group("revision"),
    )


def check_hf_library() -> Check:
    """The integrated puller needs the Python library, not a shell command."""
    try:
        installed = version("huggingface-hub")
    except PackageNotFoundError:
        return Check(
            name="Hugging Face library",
            status="fail",
            detail="The huggingface_hub Python package is not installed",
            remedy="Reinstall ENGRAI SERVER from its locked dependencies",
        )
    return Check(
        name="Hugging Face library",
        status="ok",
        detail=f"huggingface_hub {installed}; model pulls run in-process",
    )


def check_model_cache(settings: Settings) -> Check:
    """Confirm discovery includes the integrated puller's destination.

    These can drift apart: the download honours HF_HUB_CACHE/HF_HOME while
    MODEL_SEARCH_ROOTS is set independently, so weights can restore correctly
    and still be invisible to the profile editor's file picker.
    """
    roots = list(dict.fromkeys([*settings.search_roots, settings.model_download_dir]))
    present = [root for root in roots if root.is_dir()]
    if not present:
        return Check(
            name="Model cache",
            status="warn",
            detail=f"No search root exists: {', '.join(str(root) for root in roots)}",
            remedy="Set MODEL_SEARCH_ROOTS to the directory holding your GGUF files",
        )
    count = sum(1 for root in present for _ in root.rglob("*.gguf"))
    expected = settings.model_download_dir
    drift = ""
    covered = any(
        expected.resolve() == root.resolve()
        or expected.resolve().is_relative_to(root.resolve())
        for root in roots
    )
    if not covered and expected.is_dir():
        drift = f" — note the integrated puller writes to {expected}"
    return Check(
        name="Model cache",
        status="ok",
        detail=f"{count} GGUF file(s) under {', '.join(str(root) for root in present)}{drift}",
        remedy="Add that path to MODEL_SEARCH_ROOTS" if drift else "",
    )


def check_text_runtime(settings: Settings) -> Check:
    runtime = inspect_runtime(settings.runtime_dir)
    if not runtime["present"]:
        return Check(
            name="ENGRAI text runtime",
            status="fail",
            detail=f"No active runtime under {settings.runtime_dir}",
            remedy="engrai-server runtime install <engrai-runtime.tar.gz>",
        )
    if not runtime["verified"]:
        return Check(
            name="ENGRAI text runtime",
            status="fail",
            detail=runtime["error"] or "active runtime failed verification",
            remedy="Reinstall a verified bundle with: engrai-server runtime install --force <bundle>",
        )
    kernel = runtime["kernel"] or {}
    return Check(
        name="ENGRAI text runtime",
        status="ok",
        detail=(
            f"{runtime['version']} ({runtime['target']}) — "
            f"{kernel.get('name', 'unknown kernel')} {kernel.get('tag', '')}, checksums verified"
        ),
    )


def total_vram_mib() -> int:
    """Total VRAM across all CUDA devices, or 0 when it cannot be read."""
    if not shutil.which("nvidia-smi"):
        return 0
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return 0
    total = 0
    for line in output.splitlines():
        try:
            total += int(line.strip())
        except ValueError:
            continue
    return total


def check_cuda() -> Check:
    """ENGRAI workers link the CUDA runtime statically and need only the driver.

    A host with nvidia-smi but no loadable ``libcuda.so.1`` (for example a
    container without the driver libraries mounted) would start the worker
    and fail to find any GPU, so the driver library is checked explicitly.
    """
    if not shutil.which("nvidia-smi"):
        return Check(
            name="CUDA driver",
            status="warn",
            detail="No nvidia-smi; assuming a CPU-only or non-NVIDIA host",
            remedy="",
        )
    library = ctypes.util.find_library("cuda")
    if not library:
        return Check(
            name="CUDA driver",
            status="fail",
            detail="nvidia-smi is present but libcuda.so.1 cannot be loaded",
            remedy="Install or repair the NVIDIA driver packages for this distribution",
        )
    return Check(name="CUDA driver", status="ok", detail=library)


def check_gpus() -> Check:
    if not shutil.which("nvidia-smi"):
        return Check(name="GPUs", status="warn", detail="nvidia-smi unavailable")
    try:
        output = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (subprocess.SubprocessError, OSError) as exc:
        return Check(name="GPUs", status="warn", detail=f"nvidia-smi failed: {exc}")
    devices = [line.strip() for line in output.splitlines() if line.strip()]
    if not devices:
        return Check(name="GPUs", status="warn", detail="No CUDA devices reported")
    return Check(name="GPUs", status="ok", detail="; ".join(devices))


def check_profile_fit(settings: Settings) -> Check:
    """Flag routes whose weights cannot fit the GPUs on this machine.

    Deployments carry tuning (tensor splits, GPU layers, context) fitted to
    whatever hardware wrote them. Restored onto a smaller card they load and
    then crawl, because the excess silently spills to system RAM.
    This compares file size against total VRAM, which is a floor rather than a
    precise estimate: KV cache and context push real usage higher.
    """
    vram_mib = total_vram_mib()
    if not vram_mib:
        return Check(
            name="Profile fit",
            status="warn",
            detail="No CUDA devices detected; cannot check weights against VRAM",
        )
    store = ProfileStore(settings.route_dir, settings.search_roots)
    routable = [profile for profile in store.list() if profile.routable]
    if not routable:
        return Check(
            name="Profile fit",
            status="ok",
            detail=f"no routes yet; {vram_mib / 1024:.1f} GiB VRAM available",
        )
    vram_gb = vram_mib / 1024
    oversized = []
    for profile in routable:
        try:
            size_gb = profile.model_path.stat().st_size / (1024**3)
        except OSError:
            continue
        if size_gb > vram_gb:
            oversized.append(f"{profile.id} ({size_gb:.1f} GiB)")
    if oversized:
        return Check(
            name="Profile fit",
            status="warn",
            detail=(
                f"{vram_gb:.1f} GiB VRAM total; larger than that: "
                + ", ".join(oversized)
            ),
            remedy="Lower GPU layers or context for these routes, or use a smaller quant",
        )
    return Check(
        name="Profile fit",
        status="ok",
        detail=f"every route's weights fit within {vram_gb:.1f} GiB of VRAM",
    )


def check_profiles(settings: Settings) -> Check:
    store = ProfileStore(settings.route_dir, settings.search_roots)
    profiles = store.list()
    if not profiles:
        return Check(
            name="Model profiles",
            status="fail",
            detail=f"No routes in {settings.route_dir}",
            remedy="Create a route in the web UI",
        )
    missing = [profile for profile in profiles if not profile.routable]
    if missing:
        return Check(
            name="Model profiles",
            status="fail",
            detail=(
                f"{len(profiles) - len(missing)}/{len(profiles)} routable; "
                f"missing weights for: {', '.join(profile.id for profile in missing)}"
            ),
            remedy="Restore the referenced files with the model puller, or update the route in the web UI",
        )
    return Check(
        name="Model profiles",
        status="ok",
        detail=f"{len(profiles)} routable route(s)",
    )


def check_credentials(settings: Settings | None) -> Check:
    """Report the credential store's real state, not the seed variables.

    Reading Settings alone claimed "admin token set" on a bare clone that had
    no account at all, because the seeds are only ever a bootstrap input. The
    store is constructed without seeds here so the check cannot create one as
    a side effect.
    """
    if settings is None:
        return Check(
            name="Credentials",
            status="fail",
            detail="Settings could not be loaded",
            remedy="cp .env.example .env",
        )
    store = CredentialStore(settings.credentials_path)
    if store.initialized:
        profile = store.profile()
        owner = f" for {profile['name']}" if profile["name"] else ""
        return Check(
            name="Credentials",
            status="ok",
            detail=f"{len(store.list_keys())} API key(s), password set{owner}",
        )
    if settings.control_admin_token:
        return Check(
            name="Credentials",
            status="ok",
            detail="no store yet; will be seeded from ENGRAI_ADMIN_TOKEN on first start",
        )
    return Check(
        name="Credentials",
        status="warn",
        detail="no account yet",
        remedy="Open the web UI and complete first-run setup",
    )


def run() -> list[Check]:
    try:
        settings: Settings | None = get_settings()
    except Exception:
        # Settings validation fails hard when credentials are absent, which is
        # the normal state of a fresh clone. Report it as one check rather
        # than letting the whole preflight traceback.
        settings = None

    checks = [check_credentials(settings), check_hf_library(), check_cuda(), check_gpus()]
    if settings is not None:
        checks.extend(
            [
                check_text_runtime(settings),
                check_model_cache(settings),
                check_profiles(settings),
                check_profile_fit(settings),
            ]
        )
    return checks


def as_dict() -> dict[str, Any]:
    checks = run()
    return {
        "home": str(app_home()),
        "ok": all(check.status != "fail" for check in checks),
        "checks": [asdict(check) for check in checks],
    }


def main() -> int:
    report = as_dict()
    if "--json" in sys.argv:
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1

    glyphs = {"ok": "\033[32m✓\033[0m", "warn": "\033[33m!\033[0m", "fail": "\033[31m✗\033[0m"}
    print(f"\nENGRAI SERVER preflight — {report['home']}\n")
    for check in report["checks"]:
        print(f"  {glyphs[check['status']]} {check['name']:<20} {check['detail']}")
        if check["remedy"]:
            print(f"    {'':<20}   → {check['remedy']}")
    print()
    if report["ok"]:
        print("All required dependencies present.\n")
        return 0
    print("Some dependencies are missing. Run the commands above, then re-check.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
