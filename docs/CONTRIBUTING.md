# Contributing

Read [ARCHITECTURE.md](ARCHITECTURE.md) before changing models, runtimes,
networking, process ownership, or API payloads.

For running the app without changing it, start with [INSTALL.md](INSTALL.md).
For a standalone source-preview installation, see
[SOURCE-INSTALL.md](SOURCE-INSTALL.md). The commands below are for contributors,
not an end-user installation requirement.

## Work from a checkout

Install Git, uv, and Node.js/npm when editing the UI. From the repository root:

```bash
uv sync --frozen --all-extras
uv run engrai-server init
uv run engrai-server serve
```

Stop the gateway with Ctrl+C before restarting it after Python changes.
If dependencies change, run `uv sync --frozen --all-extras` again.

## Rebuild your changes

For a one-time UI rebuild, run from the checkout root:

```bash
./scripts/build-ui.sh
```

Refresh the browser if the gateway is running from this checkout. Python-only
changes need a gateway restart, not a UI build. Neither requires rebuilding
the native inference runtimes.

If you run a separately installed app, rebuilding the checkout does not update
that installation. Stop it, rebuild the UI if changed, and reinstall:

```bash
uv tool install --reinstall --python 3.14 .
```

Restart with `engrai-server serve` or, for an installed Linux service,
`engrai-server service restart`. Then refresh the browser. No commit or push
is needed to test local changes.

## Tests and packaging

```bash
uv run pytest
./scripts/build-ui.sh
uv build
```

The gateway serves the compiled UI in `src/engrai_server/static/`; include
those files alongside changes to `ui/src/`. For live UI work, run
`uv run engrai-server serve` in one terminal and `./scripts/dev.sh` in another.
The watcher does not start the gateway. See [ui/START-HERE.md](../ui/START-HERE.md).

Browser tests skip when Playwright's Chromium is unavailable. To enable them:

```bash
uv run playwright install chromium
uv run pytest tests/test_ui_smoke.py
```

Some integration tests need local model fixtures or pinned engine sources;
report skipped tests and missing prerequisites alongside results. Do not load
real models or replace active runtimes merely to run the unit test suite.

Changes must preserve these boundaries:

- end users launch `engrai-server serve` from any directory; uv, environment
  activation, and engine compilation must not become the public setup contract;
- portable data belongs under `@models`; host state belongs under XDG paths;
- core services depend on the engine protocol, not a provider implementation;
- engines remain loopback/private-IPC only;
- the UI consumes typed contracts and does not define engine flags;
- no credentials, weights, binaries, logs, local deployments, or workstation
  paths may enter Git or build artifacts;
- runtime replacement is explicit and never mutates an active worker;
- compiled UI and package artifacts must be reproducible from a clean clone.

Add tests at the contract being changed. Engine-affecting changes should name
the provider/version and include evidence for lifecycle, streaming, prompt
behavior, and intended accelerator allocation.

Use [PUBLIC-RELEASE.md](PUBLIC-RELEASE.md) when preparing the first public
repository or a release archive.
