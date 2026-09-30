# ENGRAI runtimes

This directory is the source of truth for ENGRAI's private inference
runtimes. Each lock file pins one engine at an exact commit; arbitrary system
or `PATH` binaries are never accepted by the installer.

| Lock | Runtime | Worker |
| --- | --- | --- |
| `llama.cpp.lock.json` | `engrai-text` | `bin/engrai-text-worker` (llama.cpp `llama-server`) |
| `stable-diffusion.cpp.lock.json` | `engrai-image` | `bin/engrai-image-worker` (stable-diffusion.cpp `sd-server`) |

Compiled binaries are never committed. Release bundles are built per runtime,
operating system, architecture, and backend. Until bundles are published, use
the source-build instructions in [INSTALL.md](../docs/INSTALL.md). A bundle
contains:

- the worker, built statically with the upstream web UI disabled;
- every license the worker needs under `licenses/`: upstream license files
  from the pinned source, plus the notices in `notices/<engine>/` for vendored
  code whose license lives inside a header. The build checks each notice
  against the pinned source and fails if one has gone stale;
- the exact source lock;
- `engrai-runtime.json`, including build options and SHA-256 for every payload
  file.

Fetch the pinned sources, then build a bundle:

```bash
uv run python scripts/fetch-engine-sources.py
uv run python scripts/build-runtime.py --engine llama.cpp --backend cuda
uv run python scripts/build-runtime.py --engine stable-diffusion.cpp --backend cuda
```

Release bundles use upstream's default CUDA architecture list; pass
`--cmake-option=-DCMAKE_CUDA_ARCHITECTURES=...` only for a faster local build
that targets the GPUs actually installed.
Install a bundle into the XDG runtime store. The runtime it belongs to is read
from the bundle, and text and image runtimes are activated independently:

```bash
uv run engrai-server runtime install /path/to/engrai-image-bundle.tar.gz
uv run engrai-server runtime status --runtime image
```

Replace the placeholder with the archive printed by the build. Run these
commands from the checkout root; a standalone CLI can omit `uv run`.

Backend-specific bundles are an implementation detail. Deployments target the
stable ENGRAI worker ABI; the host selects a qualified Metal, CUDA, ROCm,
Vulkan, or CPU bundle.
