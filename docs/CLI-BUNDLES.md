# Complete CLI Guide

The end-user download is one compressed archive for an OS, architecture, and backend. It includes the executable, its private Python runtime and dependencies, the console and Card Studio, and **both** verified inference engines. Model weights and personal state never ship inside the archive. Python wheels remain available for developers; they are not the end-user installation contract.

## Availability and Installation
The installer and build pipeline are implemented. Platform archives are not automatically published by committing these files. Complete the qualification and publication steps below before announcing a working download.

After publication:

```sh
curl -fsSL https://raw.githubusercontent.com/va-desai-dev/ENGRAISERVER/master/scripts/install.sh | sh
engrai-server serve
```

If PATH was updated, open a new terminal before the second command. Alternatively, run the exact command printed by the installer. Then open http://127.0.0.1:8400, create an account, and download a model in the console.

The installer requires standard shell utilities, curl, tar, and sha256sum or shasum. It never invokes Python, Git, uv, a compiler, or sudo. It checks the archive checksum before extraction and installs under `~/.local/share/engrai-server/app/` (or `$XDG_DATA_HOME/engrai-server/app/`). The stable launcher goes in `~/.local/bin/engrai-server`. Bash, zsh, and POSIX shell profiles receive an additive PATH entry only when needed; other shells can use the printed absolute command or configure PATH themselves.

Run the installer again to update, after stopping ENGRAI. Credentials, profiles, cards, lorebooks, the ledger, models, and user-installed runtimes keep their existing paths. Old app directories are retained so a process still using one is not broken. Remove the launcher and the `app/` directory to uninstall the program; do not remove the enclosing data directory unless you also intend to delete saved work.

Options can be passed after `sh -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/va-desai-dev/ENGRAISERVER/master/scripts/install.sh | sh -s -- --version v0.1.0 --backend cpu
```

Other options: `--prefix /absolute/app/path`, `--bin-dir /absolute/bin/path`, and `--no-modify-path`. Installation never starts a background service. An existing Linux service using the stable launcher picks up the update on restart. Services created from an older source installation must be reinstalled with `engrai-server service install` once to adopt the new launcher.

## Initial Build Outline

| Host | Default bundle | Build runner |
| --- | --- | --- |
| Apple Silicon Mac | `macos-aarch64-metal` | macOS 14 ARM64 |
| Intel Mac | `macos-x86_64-cpu` | macOS 15 Intel |
| Linux x86-64 | `linux-x86_64-cpu` | Ubuntu 22.04 x86-64 |
| Linux ARM64 | `linux-aarch64-cpu` | Ubuntu 22.04 ARM64 |

Additional CUDA, ROCm, or Vulkan archives use the same format and require qualified build hosts and compatible host drivers. Select one with `--backend` only when that archive is published. The initial workflow does not publish GPU Linux bundles or silently promise GPU acceleration with the CPU download.

## Runtime selection
The bundled executable locates `runtimes/` beside itself, so moving an extracted bundle is supported. With the default data layout, user-installed runtimes take precedence; otherwise the embedded engine is used. Explicit custom runtime roots remain authoritative, and a corrupt user runtime is reported rather than hidden by fallback. Runtime installs still write to the user's data store, never into the installed program. Application upgrades select a new complete directory.

## Build and Qualify (Maintainers Only)
Run the **Build complete CLI bundles** GitHub Actions workflow manually. It builds both pinned engines on each host, freezes the Python CLI with PyInstaller, checks runtime receipts after relocating the result, and uploads the complete archives and SHA-256 sidecars as workflow artifacts. It does not publish a release.

CMake progress streams into separate text-engine and image-engine build steps. The Linux archives built on Ubuntu 22.04 are also installed and run on Ubuntu 24.04 runners for both architectures. These checks use fresh user directories, block developer commands from the installed application's PATH, and verify startup, account creation, and saved-card preservation across reinstall. They do not exercise model inference or qualify every Linux distro.

For a local build, use the existing engine build workflow first:

```sh
uv sync --frozen
uv run python scripts/fetch-engine-sources.py
uv run python scripts/build-runtime.py --engine llama.cpp --backend cpu
uv run python scripts/build-runtime.py --engine stable-diffusion.cpp --backend cpu
uv run --with pyinstaller==6.22.3 python scripts/build-cli-bundle.py \
  --text-runtime /absolute/path/to/engrai-text-bundle.tar.gz \
  --image-runtime /absolute/path/to/engrai-image-bundle.tar.gz
```

Use `metal` on Apple Silicon. Both supplied runtimes must match the build host, use the same backend, satisfy the source locks, and have `GGML_NATIVE=OFF`. The output is `dist/cli/engrai-server-<os>-<arch>-<backend>.tar.gz` plus its `.tar.gz.sha256` sidecar. Build on each target OS/architecture; this is not cross-compilation. Do not commit generated bundles or engine checkouts.

Before publication, test installation with a fresh user data directory and no developer tools on PATH. Verify startup, account creation, both interfaces, text and image model loading/generation, shutdown, reinstall/update, and saved data preservation. Check bundled licenses and native shared-library dependencies. Signing/notarization for macOS distribution remains pending as a next step.

Attach the complete archives and their checksum sidecars to a reviewed GitHub release. Keep the filenames unchanged: the installer selects by OS/architecture and backend. The default installer uses GitHub's latest published release; a prerelease is usable through `--version TAG`. Publish all advertised targets together, and verify the public install command before announcing availability.

Build references: [PyInstaller bundles](https://pyinstaller.org/en/stable/operating-mode.html) and [runtime paths](https://pyinstaller.org/en/stable/runtime-information.html).
