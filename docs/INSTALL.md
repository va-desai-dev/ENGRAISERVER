# Running ENGRAISERVER

ENGRAISERVER and the ENGRAI client apps exist to **simplify local LLM accessibility**. Normal use means launching an installed app, choosing a model, and connecting a client—not learning Python environments or compiling inference engines.

## Installation

The CLI installer downloads a complete platform bundle: ENGRAI, its private Python runtime, the web interface, and both text and image inference engines. You do not need Git, Python, uv, Node.js, or a compiler to just run it at default.

The installer and build workflow are implemented, but release archives still need to be qualified and published. Until then, use the developer-only [source installation](SOURCE-INSTALL.md). See [CLI bundles](CLI-BUNDLES.md) for the release matrix and exact publication requirements.

Run this command in the terminal to install the pinned release version:

```bash
curl -fsSL https://raw.githubusercontent.com/va-desai-dev/ENGRAISERVER/master/scripts/install.sh | sh
```

The installer handles file placement and first-time initialization. Open a new terminal if it reports a PATH change. To update, stop the server, rerun the installer, then restart the server. Saved work is preserved.

## Launching ENGRAISERVER

After installation, open a terminal in any directory:

```bash
engrai-server serve
```

For source installations only, run `engrai-server init` once first; it preserves existing configuration. No `cd`, directory path, `uv run`, or environment activation is required.

Open **http://127.0.0.1:8400** and create your administrator account. Leave the terminal open while using the server; Ctrl+C stops it. There is no default password or API key. Do not copy placeholder credentials from `.env.example`.

The web interface is included in the installed app. Node.js is only a developer dependency for rebuilding that interface.

## Runtime Status
The gateway can start without inference runtimes or models, but it cannot generate until both are available. Check the current installation with:

```bash
engrai-server runtime status
engrai-server runtime status --runtime image
engrai-server preflight
```

Standalone archives embed both runtimes, and a fresh installation discovers them automatically. No separate runtime installation is needed. An explicitly installed runtime in the user's data directory takes precedence. Source-only installs still need the developer workflow in [RUNTIMES.md](RUNTIMES.md).

## Add or Download Models
1. Under **Download models**, search for a GGUF model by name or paste its Hugging Face page. Choose a repository, then a **Quantization/variant**. Split files are grouped together. Click **Review download**, check the size, then confirm. **Download all variants** is optional and may be very large. These are GGUF file choices, not a guarantee of runtime compatibility; auxiliary projectors are excluded. For other files, use **Advanced**. For existing downloads, configure search directories under **Profile and access**.
2. Under **Text Models**, choose **New Profile**, select the model and deployment settings, save, then load it. Under **Image Models**, use **New Profile** and provide the required checkpoint, encoder, and VAE. See [DIFFUSION-MODELS.md](DIFFUSION-MODELS.md).
3. Create a client API key under **Profile and access** and retain the revealed secret. It is distinct from the administrator password.
4. In your ENGRAI client or another compatible client, use the server endpoint and API key. The OpenAI-compatible base URL on the server itself is `http://127.0.0.1:8400/v1`; the request's `model` value is the route ID. On another device, use a reachable server address, not that device's own `127.0.0.1`.

> [!CAUTION]
> Private or gated downloads currently need a scoped read token in the server's `HF_TOKEN` environment variable. Put it in the private `gateway.env` file and restart. The token does not enter the browser.

## Terminal chat and profile control

Use the existing `engrai-server` command to talk to a running gateway without
opening the web interface. Use a saved text route ID from `engrai-server routes`.
On the gateway's own host, logged in as the account that runs the service
(including over SSH), no credentials are needed:

```bash
engrai-server load my-profile
engrai-server chat my-profile
engrai-server unload
```

Each time it starts, the gateway writes a fresh token to `local-cli.token` next
to `credentials.json`, readable only by the service account, and removes it on
shutdown. The CLI uses that token when it targets a gateway on loopback and was
given no explicit credential. A user who can read that file can already read
the credential store, so an API key would add no protection there. Loopback
connections alone are not trusted, because a reverse proxy such as cloudflared
also connects from `127.0.0.1`.

From another machine, or as a different local user, pass credentials:

```bash
export ENGRAI_SERVER_URL='https://your-gateway'
export ENGRAI_API_KEY='your-client-api-key'
export ENGRAI_ADMIN_TOKEN='your-admin-password-or-session-token'
```

Remotely, `chat` needs only the client API key. Its first message lets the gateway load
the requested profile through the normal inference path; a separate `load` is
optional. `load` and `unload` need the administrator password or session token.
These controls operate on the gateway's shared text worker, so switching or
unloading a profile also affects other clients using that worker. They do not
stop the gateway or replace its runtime.

Replies stream as they arrive. When the model thinks, its reasoning is shown
first in muted text, as the gateway separates it into `reasoning_content`. It is
never added to the conversation history. Pass `--hide-thinking` or use
`/think` to hide it; it is also omitted when stdout is redirected, and
`NO_COLOR` turns off the muting. Each terminal chat keeps its own conversation
in memory and sends the full message history with each request. Nothing is
saved to disk or resumed after exit. Failed or interrupted replies are excluded
from subsequent context; already printed partial output may remain visible.
History is not automatically truncated: use `/clear` if the conversation
outgrows the profile's context window.

| Chat command | Effect |
| --- | --- |
| `/models` | List available text route IDs from the connected gateway. |
| `/load PROFILE` | Load/select a profile; changing profiles clears this chat's history. Requires admin credentials. |
| `/unload` | Unload the shared text worker and clear this chat. Use `/load PROFILE` to continue. Requires admin credentials. |
| `/clear` | Start fresh while retaining the `--system` instruction. |
| `/think` | Show or hide the model's reasoning for later replies. |
| `/help` | Show available commands. |
| `/exit` or `/quit` | Exit without unloading the model or stopping the server. |

Ctrl+C during generation cancels the local reply and returns to the prompt.
Ctrl+C at the prompt or Ctrl+D exits. Interactive input is one message per line;
use redirected stdin for a single multiline prompt:

```bash
engrai-server chat my-profile --system 'Answer concisely.'
engrai-server chat my-profile --no-stream < prompt.txt
engrai-server chat my-profile --url https://your-gateway.example
```

`--url` (or `ENGRAI_SERVER_URL`) takes the gateway root, **without `/v1`**.
Otherwise the client uses the configured bind host and port; wildcard bind
addresses connect over loopback. `--host` and `--port` also work for these
commands. `--api-key` and `--admin-token` override the credential environment
variables; prefer environment variables to keep secrets out of process
arguments. `--timeout SECONDS` sets the HTTP inactivity timeout (600 seconds
by default). `load --json` and `unload --json` print the gateway response for
scripts. Piped chat writes only the reply to stdout and reports errors on stderr
with a nonzero exit status.

## Background Service
On **Linux ONLY**, stop the foreground gateway first, then run from any directory:

```bash
engrai-server service install --now
loginctl enable-linger "$USER"
```

Lingering keeps the service alive after logout; local policy may require administrator approval. Everyday control:

```bash
engrai-server service status
engrai-server service restart
engrai-server service stop
```

The service records the absolute path to the installed command. Re-run `service install --now` if an installation change moves that command. Restart after changing `gateway.env`. For logs, run `journalctl --user -u engrai-server -f`.

## Access Remotely

The default listener is local to the server. For initial remote setup, run this on your client computer, replacing `user@server` with your SSH destination:

```bash
ssh -N -L 8400:127.0.0.1:8400 user@server
```

Leave the tunnel running and open http://127.0.0.1:8400 on the client. If that local port is occupied, use `-L 8401:127.0.0.1:8400` and open port 8401. For persistent access, configure a trusted private-network listener or an HTTPS reverse proxy. `serve --host` changes the listener but does not add TLS. Keep worker ports 5002/5003 private; see [SECURITY.md](../SECURITY.md).

## Where your data lives

Run `engrai-server config` to see the actual paths. The defaults are independent of the directory from which you launch the app:

| Data | Directory |
| --- | --- |
| Configuration and routes | `~/.config/engrai-server/` |
| Installed app, user runtimes, Card Studio library | `~/.local/share/engrai-server/` |
| Credentials, logs, process state | `~/.local/state/engrai-server/` |
| Disposable cache | `~/.cache/engrai-server/` |

Advanced installations can override XDG roots or set `ENGRAI_HOME` to an absolute directory consistently for every command. No override is needed for normal use. Back up configuration, state, and the `cardgen/` data directory before updates. Source-preview update instructions are in [SOURCE-INSTALL.md](SOURCE-INSTALL.md#updates-and-local-changes).

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
