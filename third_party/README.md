# Third-party source

Engine sources are not stored in this repository. Each engine's repository and
full commit are pinned in `runtime/<engine>.lock.json`, and this command
fetches them into `third_party/<engine>/`, verifying every revision:

```bash
uv run python scripts/fetch-engine-sources.py
```

| Engine | Lock | Runtime | License |
| --- | --- | --- | --- |
| llama.cpp | `runtime/llama.cpp.lock.json` | `engrai-text` | MIT |
| stable-diffusion.cpp | `runtime/stable-diffusion.cpp.lock.json` | `engrai-image` | MIT |

stable-diffusion.cpp's lock also pins the submodules the build needs (its
patched `ggml` and `libwebp`); its upstream web UI and WebM support are never
fetched or built. `scripts/build-runtime.py` refuses a checkout at any other
revision. Fetched engine directories under `third_party/` are ignored by Git;
this README is tracked. Run the command from the repository root after
`uv sync --frozen`.
