# Start here

This folder contains only the browser interface. The gateway serves the
compiled copy in `src/engrai_server/static/`. From the repository root, run
`./scripts/dev.sh` to rebuild it on every save while the gateway keeps
running, or `./scripts/build-ui.sh` for a one-off production build.

For first-time setup, follow [the installation guide](../docs/INSTALL.md).
UI development additionally requires Node.js/npm. From the repository root:

```bash
uv sync --frozen --all-extras
uv run engrai-server serve
```

Leave that terminal running. In a second terminal, from the same root:

```bash
./scripts/dev.sh
```

Open http://127.0.0.1:8400. The watcher installs UI dependencies when needed
and rebuilds the files served by the gateway; it does not start a separate
web server. Include `src/engrai_server/static/` with UI source changes.

## The four places to edit

- `src/modes/Control.tsx` — the main authenticated screen and its section order.
- `src/styles/app.css` — component layout, shapes, and visual treatment.
- `src/styles/tokens.css` — colors, typography, spacing, and design constants.
- `src/components/` — individual interface pieces such as the header, cards,
  account panel, and model editor.

`src/modes/Display.tsx` is the separate read-only display experience.
`src/App.tsx` chooses between the control and display experiences.
`public/` contains the installable-app icon and manifest.

Everything else here is build configuration (`package.json`, `tsconfig.json`,
`vite.config.ts`) or installed dependencies (`node_modules/`, not committed).
