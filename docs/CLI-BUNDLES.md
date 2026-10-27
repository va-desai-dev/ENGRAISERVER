# Complete CLI Guide

The end-user download is one compressed archive for an OS, architecture, and backend. It includes the executable, its private Python runtime and dependencies, the console and Card Studio, and **both** verified inference engines. Model weights and personal state never ship inside the archive. Python wheels remain available for developers; they are not the end-user installation contract.

## Availability and Installation
Pushes to `master`, pull requests targeting `master`, and manual workflow runs build fresh platform archives in GitHub Actions. Pushing a version tag such as `v1.0.0` also attaches the checked archives to a draft GitHub release. Publish that draft after qualification to make it available to the installer; branch pushes do not replace an existing release or update an installed app.

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
curl -fsSL https://raw.githubusercontent.com/va-desai-dev/ENGRAISERVER/master/scripts/install.sh | sh -s -- --version v1.0.0 --backend cpu
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
The **Build complete CLI bundles** GitHub Actions workflow runs automatically on pushes to `master`, pull requests targeting `master`, and `v*` tags. It can also be run manually. It checks version consistency, rebuilds the UI with Node.js 22 and the committed npm lockfile, and runs the application tests before building both pinned engines on each host. The CLI packager always rebuilds the UI, checks that every console and Card Studio web file matches the checkout, freezes the Python CLI with PyInstaller, checks runtime receipts after relocating the result, and uploads the complete archives and SHA-256 sidecars as workflow artifacts. Download artifacts from the run for the commit you want to test.

CMake progress streams into separate text-engine and image-engine build steps. The Linux archives built on Ubuntu 22.04 are also installed and run on Ubuntu 24.04 runners for both architectures. These checks use fresh user directories, block developer commands from the installed application's PATH, and verify startup, account creation, and saved-card preservation across reinstall. They do not exercise model inference or qualify every Linux distro.

For a local build, install Node.js 22/npm and uv, then use the existing engine build workflow first:

```sh
uv sync --frozen
uv run --frozen python scripts/check-version.py
uv run python scripts/fetch-engine-sources.py
uv run python scripts/build-runtime.py --engine llama.cpp --backend cpu
uv run python scripts/build-runtime.py --engine stable-diffusion.cpp --backend cpu
uv run --with pyinstaller==6.22.3 python scripts/build-cli-bundle.py \
  --text-runtime /absolute/path/to/engrai-text-bundle.tar.gz \
  --image-runtime /absolute/path/to/engrai-image-bundle.tar.gz
```

Use `metal` on Apple Silicon. Both supplied runtimes must match the build host, use the same backend, satisfy the source locks, and have `GGML_NATIVE=OFF`. The output is `dist/cli/engrai-server-<os>-<arch>-<backend>.tar.gz` plus its `.tar.gz.sha256` sidecar. Build on each target OS/architecture; this is not cross-compilation. Do not commit generated bundles or engine checkouts.

Before publication, test installation with a fresh user data directory and no developer tools on PATH. Verify startup, account creation, both interfaces, text and image model loading/generation, shutdown, reinstall/update, and saved data preservation. Check bundled licenses and native shared-library dependencies. Signing/notarization for macOS distribution remains pending as a next step.

## Versioning and releasing

`pyproject.toml` defines the application version. The API, CLI, and bundle manifest read the installed package metadata. Keep `ui/package.json`, both root version entries in `ui/package-lock.json`, the application entry in `uv.lock`, and the default `OCI_VERSION` in `Containerfile` aligned. `scripts/check-version.py` rejects mismatches; tagged builds additionally require the tag to equal `v` plus the application version. Native engine versions in `runtime/*.lock.json` identify independently pinned engines and do not change for an application-only release.

For the 1.0.0 release, commit and push the version and source changes, then tag that commit:

```sh
uv run --frozen python scripts/check-version.py --tag v1.0.0
git tag -a v1.0.0 -m "ENGRAISERVER v1.0.0"
git push origin v1.0.0
```

After all four platform builds and both Linux compatibility checks pass, the tag workflow verifies the archive checksums and creates or updates the `v1.0.0` draft with all eight assets. An existing empty draft is supported. A rerun may replace draft assets, but the workflow refuses to overwrite a published release. For an existing tag, select that tag when running the workflow manually to rebuild its exact source.

Plain `vX.Y.Z` tags are explicitly marked as stable releases, including when an existing draft was previously marked as a prerelease. Tags with preview suffixes remain prereleases. Draft status only means unpublished; it does not mean prerelease. When publishing a stable draft, leave **Set as a pre-release** unchecked and select **Set as the latest release** so the default installer finds it.

Review and publish the complete draft after qualification. Keep the filenames unchanged: the installer selects by OS/architecture and backend. The default installer uses GitHub's latest published release; a prerelease is usable through `--version TAG`. Publish all advertised targets together, and verify the public install command before announcing availability. Users update by stopping ENGRAI, rerunning the installer, and restarting it.

Build references: [PyInstaller bundles](https://pyinstaller.org/en/stable/operating-mode.html) and [runtime paths](https://pyinstaller.org/en/stable/runtime-information.html).
