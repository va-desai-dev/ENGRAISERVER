"""Entry point that takes its listen address from configuration.

The systemd unit used to carry a literal `--host <address>` on its ExecStart
line, which meant it only worked on the one machine holding that address:
anywhere else uvicorn exits immediately with "Cannot assign requested
address". Reading the address from the same env file as everything else keeps
one source of truth and lets the unit be copied to any host unchanged.

    engrai-server serve
"""

from __future__ import annotations



def main() -> None:
    """Kept so the systemd unit and older habits keep working.

    `engrai-server serve` is the same thing with flags.
    """
    from .cli import main as cli_main

    raise SystemExit(cli_main(["serve"]))


if __name__ == "__main__":
    main()
