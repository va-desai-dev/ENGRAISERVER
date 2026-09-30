# Source installation — preview testers and developers

This is a temporary source-stage setup path, not the intended end-user
installation experience. See [Running ENGRAISERVER](INSTALL.md) for everyday
use. App installers and prebuilt ENGRAI runtime downloads are still release
work; do not assume a published PyPI package or native release is available.

## Install an independent app command

Install Git and [uv](https://docs.astral.sh/uv/getting-started/installation/).
With an existing checkout, skip cloning and run the install commands from its
root. Otherwise, once the repository is published:

```bash
git clone https://github.com/va-desai-dev/ENGRAISERVER.git
cd ENGRAISERVER
```

Install once:

```bash
uv tool install --python 3.14 .
uv tool update-shell
```

An absolute checkout path can replace `.` to install from another directory.
uv manages the isolated Python environment; you do not activate it manually.
The package supports Python 3.12+; the checkout currently pins 3.14. The
compiled UI is included, so Node.js is not needed for this installation.

Open a new terminal if needed, then use the app from any directory:

```bash
engrai-server init
engrai-server serve
```

This is a non-editable install: source changes do not alter the installed app
until you reinstall it. Configuration, credentials, and runtimes live outside
the checkout. Avoid starting two gateways against the same application state.

## Updates and local changes

Stop the gateway or service and back up the config/state paths reported by
`engrai-server config`. From the updated checkout, reinstall:

```bash
uv tool install --reinstall --python 3.14 .
```

If you changed UI source, run `./scripts/build-ui.sh` **before** reinstalling;
that build needs Node.js/npm. Then restart with `engrai-server serve`, or
`engrai-server service restart` if you installed the Linux service. Refresh
the browser. If the installed command moved, run `service install --now`
again so the service points at the correct installation.

See [Contributing](CONTRIBUTING.md) for checkout-based development and UI
watch mode. Python/UI changes do not require native engine compilation.

## Preview runtime setup

The app installation above does not include a native inference runtime.
Install a compatible ENGRAI bundle supplied by a maintainer, or follow the
developer build instructions in [RUNTIMES.md](RUNTIMES.md). That build process
is separate from normal app use and must not become a prerequisite for the
eventual public installation experience.
