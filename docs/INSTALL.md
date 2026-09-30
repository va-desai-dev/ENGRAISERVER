# Running ENGRAISERVER

ENGRAISERVER and the ENGRAI client apps exist to **simplify local LLM
accessibility**. Normal use means launching an installed app, choosing a
model, and connecting a client—not learning Python environments or compiling
inference engines.

## Installation availability

The current preview supports Linux and macOS, but a public app installer and
downloadable ENGRAI runtime bundles are not yet provided. There is not yet a
complete no-toolchain installation path to recommend to end users.

If you are testing this source-stage preview, follow
[Source installation](SOURCE-INSTALL.md) once. Those developer tools are a
temporary preview requirement, not the intended product experience. A public
installer must provide the app command and a compatible verified runtime
without requiring users to install uv, activate an environment, or use CMake.

## Launch from anywhere

After installation, open a terminal in any directory:

```bash
engrai-server init
engrai-server serve
```

`init` is first-time setup; it preserves existing configuration. For everyday
launches, only `engrai-server serve` is needed. No `cd`, directory path,
`uv run`, or environment activation is required.

Open **http://127.0.0.1:8400** and create your administrator account. Leave the
terminal open while using the server; Ctrl+C stops it. There is no default
password or API key. Do not copy placeholder credentials from `.env.example`.

The web interface is included in the installed app. Node.js is only a
developer dependency for rebuilding that interface.

## Runtime readiness

The gateway can start without inference runtimes or models, but it cannot
generate until both are available. Check the current installation with:

```bash
engrai-server runtime status
engrai-server runtime status --runtime image
engrai-server preflight
```

Text and image generation use separate runtimes; text does not require the
image runtime. Automatic runtime acquisition is not implemented yet. During
this preview, missing runtimes require a compatible bundle supplied by a
maintainer or the developer build workflow in [RUNTIMES.md](RUNTIMES.md).
Compiling engines is not the intended end-user setup path.

## Add a model and connect a client

1. Under **Download models**, search for a GGUF model by name or paste its
   Hugging Face page. Choose a repository, then a **Quantization / variant**.
   Split files are grouped together. Click **Review download**, check the size,
   then confirm. **Download all variants** is optional and may be very large.
   These are GGUF file choices, not a guarantee of runtime compatibility;
   auxiliary projectors are excluded. For other files, use **Advanced**.
   For existing downloads, configure search directories under **Profile and access**.
2. Create a **New route**, select the model and deployment settings, save,
   then load it. For images, use **New image model** and provide the required
   checkpoint, encoder, and VAE. See [DIFFUSION-MODELS.md](DIFFUSION-MODELS.md).
3. Create a client API key under **Profile and access** and retain the revealed
   secret. It is distinct from the administrator password.
4. In your ENGRAI client or another compatible client, use the server endpoint
   and API key. The OpenAI-compatible base URL on the server itself is
   `http://127.0.0.1:8400/v1`; the request's `model` value is the route ID.
   On another device, use a reachable server address, not that device's own
   `127.0.0.1`.

Private or gated downloads currently need a scoped read token in the server's
`HF_TOKEN` environment variable. Put it in the private `gateway.env` file and
restart. The token does not enter the browser.

## Background service

On Linux, stop the foreground gateway first, then run from any directory:

```bash
engrai-server service install --now
loginctl enable-linger "$USER"
```

Lingering keeps the service alive after logout; local policy may require
administrator approval. Everyday control:

```bash
engrai-server service status
engrai-server service restart
engrai-server service stop
```

The service records the installed command's absolute location. Re-run
`service install --now` if an installation change moves that command. Restart
after changing `gateway.env`. For logs, run
`journalctl --user -u engrai-server -f`.

macOS currently supports foreground operation or your own supervisor. These
service commands require Linux/systemd; native launchd integration is not
implemented yet.

## Access from another computer

The default listener is local to the server. For initial remote setup, run
this on your client computer, replacing `user@server` with your SSH destination:

```bash
ssh -N -L 8400:127.0.0.1:8400 user@server
```

Leave the tunnel running and open http://127.0.0.1:8400 on the client. If that
local port is occupied, use `-L 8401:127.0.0.1:8400` and open port 8401.
For persistent access, configure a trusted private-network listener or an
HTTPS reverse proxy. `serve --host` changes the listener but does not add TLS.
Keep worker ports 5002/5003 private; see [SECURITY.md](../SECURITY.md).

## Where your data lives

Run `engrai-server config` to see the actual paths. The defaults are independent
of the directory from which you launch the app:

| Data | Directory |
| --- | --- |
| Configuration and routes | `~/.config/engrai-server/` |
| Runtime bundles | `~/.local/share/engrai-server/` |
| Credentials, logs, process state | `~/.local/state/engrai-server/` |
| Disposable cache | `~/.cache/engrai-server/` |

Advanced installations can override XDG roots or set `ENGRAI_HOME` to an
absolute directory consistently for every command. No override is needed for
normal use. Back up configuration and state before updates. Source-preview
update instructions are in [SOURCE-INSTALL.md](SOURCE-INSTALL.md#updates-and-local-changes).

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `engrai-server` command not found | Confirm installation completed and open a new terminal. See the source-preview guide if no installer is available. |
| UI works but loading fails | Run `engrai-server runtime status` and `engrai-server preflight`; verify runtime and model availability. |
| Model picker is empty | Configure search directories or finish a filtered download, then reopen the editor. |
| Port 8400 is occupied | Stop the other gateway or use `engrai-server serve --port 8401`. |
| Remote device cannot connect | Use the SSH tunnel above or configure the gateway listener/proxy. |
| Environment edits do not apply | Restart the gateway; exported variables can override the file. |
| Service stops after logout | Check `loginctl show-user "$USER" -p Linger`. |
