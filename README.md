<div align="center">
  
# ENGRAISERVER 
### Simplified Hosting for Local Inference Models

<div align="center">

[![Linux](https://img.shields.io/badge/For_Linux-FCC624?logo=linux&logoColor=black)](#)
[![macOS](https://img.shields.io/badge/For_macOS-000000?logo=apple&logoColor=F0F0F0)](#)
[![CUDA](https://img.shields.io/badge/CUDA_Built-76B900?logo=nvidia&logoColor=fff)](#)
[![MLX](https://custom-icon-badges.demolab.com/badge/MLX_Built-151E29?logo=mlx&logoColor=fff)](#)
[![Hugging Face](https://img.shields.io/badge/Hugging%20Face_Friendly-FFD21E?logo=huggingface&logoColor=000)](#)
[![Cloudflare](https://img.shields.io/badge/Cloudflare_Ready-F38020?logo=Cloudflare&logoColor=white)](#)
  
</div>

</div>

Simplify local LLM accessibility. ENGRAISERVER runs your local models and connects them to client apps that accept image and chat completions. Installing and using it should feel like using an app—not setting up a development environment.

## Features
1. **Blistering Speed:** Powered by llama.cpp & Diffusions, modified, tuned, and tested for long-context generation speeds.
2. **NVIDIA? AMD? CPU? Metal?** ENGRAISERVER does not judge! It supports Metal for MLX, CUDA, and Vulkan across all hardware.
3. **Model Browser:** To download models directly in-app via Hugging Face.
4. **Remote Hosting?** Got you covered with setup guides for Tailscale (WireGuard) & Cloudflare Tunnels.
5. **Card Studio:** Create character, group, scenario, and lorebook cards with images to be exported as SillyTavern CardV2 formatted JSON or PNG. See [CARD-STUDIO.md](docs/CARD-STUDIO.md).

## Quickstart Guide
**Download ENGRAISERVER's latest release** directly from GitHub by pasting this into your Terminal for macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/va-desai-dev/ENGRAISERVER/master/scripts/install.sh | sh -s -- --version v0.1.0
```

> *Release availability: The installer and bundle build workflow are in this repository. You must install the exact version. See [CLI bundles](docs/CLI-BUNDLES.md). Check the latest release at [Releases](https://github.com/va-desai-dev/ENGRAISERVER/releases)*

**Verify** the downloaded version matches the latest release by pasting this command:

```sh
engrai-server -h #list all the commands and help guides
engrai-server --version #check version installed
```

**Instantiate** the server at localhost:8400 **(default port for ENGRAISEREVR)** by running the command below:

```sh
engrai-server --serve #launch command on default port
```

Open **http://127.0.0.1:8400** in any web browser on the same device and create your administrator account.


>Standalone bundles include the text and image engines; model weights download separately through the console. Source contributors can still use the [source installation guide](docs/SOURCE-INSTALL.md) and build engines using [RUNTIMES.md](docs/RUNTIMES.md).

> See [Running ENGRAISERVER](docs/INSTALL.md) for models, client connections, background operation, and troubleshooting.

## Configuration
Optionally, you may wish to implement the following quickstart configuration flags for advanced initialization:

| Flag | Purpose | Command |
|---|---|---|
|`--serve`|Default startup|`engrai-server --serve`|
|`--init`|Creates a new data cache|`engrai-server --init`|
|`--config`|Showing & modifying settings|`engrai-server --config`|
|`--routes`|List your saved model files|`engrai-server --preflight`|

### Running as Background Service (LINUX ONLY)
The quicklaunch `engrai-server serve` command stops when the terminal or SSH session that started it closes, for example, when the laptop you connected from goes to sleep. For an always-on host, stop the foreground gateway with Ctrl+C first, then install the systemd user service once using the installed app:

```bash
engrai-server service install --now
loginctl enable-linger "$USER"
```

The first command writes `~/.config/systemd/user/engrai-server.service`, enables it at boot, and starts it now. The second keeps your user services running when nobody is logged in; you only need to run it once per machine.

---

## Advanced User Guides
### Documentation Index
Access the comprehensive list of documentation, guides, and FAQs for various configurations, troubleshooting, and more:

|Guide|Document|
|:---|---:|
|Everyday commands, models, and services.|[**INSTALLATION**](docs/INSTALL.md)|
|Complete archives, updates, and maintainer builds.|[**ARCHIVES**](docs/CLI-BUNDLES.md)|
|Checkout-based development setup.|[**SOURCE INSTALL**](docs/SOURCE-INSTALL.md)|
|Tests, UI guides, and policies for contributors.|[**CONTRIBUTING**](docs/CONTRIBUTING.md)|
|Build and deploy your own images.|[**DOCKER PODS**](deploy/CONTAINERS.md#build-an-image)|
|Deploy remote cloud compute clusters via Kubernetes.|[**KUBERNETES**](deploy/CONTAINERS.md#kubernetes-pod)|
|Learn more about the architecture.|[**ARCHITECTURE**](docs/ARCHITECTURE.md)|
|Configurate & change runtime hosts, port, and settings.|[**RUNTIMES**](docs/RUNTIMES.md)|
|Learn more about the text-inference engine and torch flags.|[**LLM PROFILES**](docs/LLM-PROFILES.md)|
|Learn more about the image-gen engine.|[**DIFFUSIONS**](docs/DIFFUSION-MODELS.md)|

### Repository layout
Use this as a guide to understanding what each subfolder and file broadly does inside this codebase.
| Path | Responsibility |
| --- | --- |
| `@models/` | Portable manifests and versioned schemas; no host paths or GPU assignments |
| `src/engrai_server/domain/` | Engine-independent model and deployment contracts |
| `src/engrai_server/engines/` | Engine protocol and provider adapters |
| `src/engrai_server/` | Gateway, CLI, services, authentication, and orchestration |
| `src/engrai_server/static/` | Compiled presentation layer shipped in the wheel |
| `src/engrai_server/cardgen/` | Card Studio: its API, library and job buffer; `web/` is its unbuilt browser app |
| `ui/` | Presentation source only |
| `tests/` | Domain, lifecycle, API, packaging, and UI tests |

### Cache Folder
After you launch ENGRAISERVER, you will notice `engrai-server` in your hidden `./cache`, `./config`, `./local` system folders. The following table explains what each file/folder in the cache stores:
| Kind | Default |
| --- | --- |
| Configuration and deployments | `${XDG_CONFIG_HOME:-~/.config}/engrai-server/` |
| Versioned runtime bundles and resolved artifacts | `${XDG_DATA_HOME:-~/.local/share}/engrai-server/` |
| Card Studio library (cards, prompts, galleries) | `${XDG_DATA_HOME:-~/.local/share}/engrai-server/cardgen/` |
| Credentials, logs, and process state | `${XDG_STATE_HOME:-~/.local/state}/engrai-server/` |
| Disposable cache | `${XDG_CACHE_HOME:-~/.cache}/engrai-server/` |
| Hugging Face model cache | `${HF_HUB_CACHE:-${HF_HOME:-~/.cache/huggingface}/hub}` |

## Contribution & Development
* Contributor tooling is separate from the installed-app experience. See [CONTRIBUTING.md](docs/CONTRIBUTING.md) for checkout setup, rebuilding local changes, UI watch mode, tests, and packaging. 
* Use [SOURCE-INSTALL.md](docs/SOURCE-INSTALL.md) to install a source-preview build as an independent app command.
* Native engine compilation and qualification belong to [RUNTIMES.md](docs/RUNTIMES.md). They are maintainer workflows, not something users should need to learn to run local models.
* 
## Security & Privacy Guards
- The fresh default binds ENGRAISERVER to `127.0.0.1:8400`.
- Engine workers remain on loopback-only and do not write scripts outside the local repository.
- **engraiAuth Keys** are stored with mode `0600` under XDG state for secure local storage. *It is your responsibility to keep your credentials secure and private to prevent unauthorized access.* 
- Model manifests do not encode secrets, absolute paths, or device information when routed via network payloads. Credentials and secured data remain seperated from the network API.
- Runtime installation is explicit, checksums every payload file, and uses an atomic active-runtime record; automatic replacement while requests are running is forbidden.
- See [SECURITY](SECURITY.md) & [ARCHITECTURE](docs/ARCHITECTURE.md) for reporting and detailed operational guidance.

## Licensing
Copyright © 2026 ENGRAI by Vedant A. Desai. All rights reserved.

> ENGRAISERVER is pre-release software, licensed under the GNU Affero General Public License v3.0. See [LICENSE.md](LICENSE.md). Bundled and pinned third-party components keep their own licenses; see [docs/ACKNOWLEDGEMENTS.md](docs/ACKNOWLEDGEMENTS.md).

<div align="center">
  
[![Join ENGRAI's Discord](https://img.shields.io/badge/Discord-%235865F2.svg?&logo=discord&logoColor=white)](https://discord.gg/TVB6nHKuN)
[![Email Us](https://img.shields.io/badge/email_us-red)](mailto:support@engrai.app)

</div>