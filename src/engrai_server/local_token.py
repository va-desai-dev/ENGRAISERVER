"""Owner-only token that lets the CLI on this host skip API keys.

The gateway mints a fresh random token each time it starts and writes it next
to credentials.json, readable only by the account that runs the service. The
terminal client reads it when the user gave no explicit credential.

Possessing this file proves the same thing as being able to read
credentials.json or gateway.env, or to restart the unit: a shell as the
service owner. Asking that user for an API key protects nothing.

This deliberately does not trust loopback connections instead. cloudflared and
other reverse proxies reach the gateway from 127.0.0.1, so "came from
localhost" says nothing about who is on the other end.
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

LOCAL_TOKEN_PREFIX = "engl"
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def local_token_path(credentials_path: Path) -> Path:
    return credentials_path.with_name("local-cli.token")


def write_local_token(path: Path) -> str:
    """Mint a token and write it 0600, atomically, before anything can read it."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    secret = f"{LOCAL_TOKEN_PREFIX}-{secrets.token_urlsafe(32)}"
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(secret + "\n")
    os.replace(temporary, path)
    return secret


def remove_local_token(path: Path, secret: str) -> None:
    """Remove the file on shutdown, unless a newer gateway already replaced it."""
    try:
        if path.read_text(encoding="utf-8").strip() == secret:
            path.unlink()
    except OSError:
        pass


def read_local_token(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None
