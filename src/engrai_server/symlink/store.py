"""Private server identity and paired public keys; invitations never reach disk."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from .crypto import decode_key, encode

INVITE_TTL = 300
MAX_INVITES = 8
MAX_PEERS = 128


@dataclass
class Invitation:
    id: str
    label: str
    secret: bytes = field(repr=False)
    expires_at: float
    deadline: float


def endpoint_url(value: str) -> str:
    parts = urlsplit(value)
    if (parts.scheme not in {"http", "https"} or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or parts.path not in {"", "/"}
            or any(char.isspace() for char in value)):
        raise ValueError("Use the server's reachable http(s) origin without a path or credentials")
    _ = parts.port  # Validate port syntax and range, without resolving or contacting the host.
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc,
                       "/symlink/v1", "", ""))


class PairingStore:
    def __init__(self, path: Path):
        self.path = path
        self.invitations: dict[str, Invitation] = {}
        self._mutex = threading.RLock()

    @contextmanager
    def _locked(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "r+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            private = X25519PrivateKey.generate().private_bytes_raw()
            data = {"version": 1, "private_key": encode(private), "peers": {}}
            self._write(data)
        # Corruption must fail closed, never silently replace a pinned identity.
        if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("peers"), dict):
            raise ValueError("Invalid SymLink state")
        decode_key(data["private_key"])
        for peer_id, peer in data["peers"].items():
            if peer.get("id") != peer_id:
                raise ValueError("Invalid SymLink peer")
            decode_key(peer["public_key"])
        return data

    def _write(self, data: dict) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".symlink-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def identity(self) -> tuple[bytes, str]:
        with self._locked():
            private = decode_key(self._read()["private_key"])
        public = X25519PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
        return private, encode(public)

    def peers(self) -> list[dict]:
        with self._locked():
            return list(self._read()["peers"].values())

    def peer(self, peer_id: str) -> dict:
        with self._locked():
            return self._read()["peers"][peer_id]

    def revoke(self, peer_id: str) -> None:
        with self._locked():
            data = self._read()
            del data["peers"][peer_id]
            self._write(data)

    def invite(self, label: str, origin: str) -> dict:
        endpoint = endpoint_url(origin)
        _, server_key = self.identity()
        with self._mutex:
            self._expire()
            if len(self.invitations) >= MAX_INVITES:
                raise ValueError("Cancel an invitation before creating another")
            invitation = Invitation(secrets.token_hex(16), label, secrets.token_bytes(32),
                                    time.time() + INVITE_TTL, time.monotonic() + INVITE_TTL)
            self.invitations[invitation.id] = invitation
        return {"version": 1, "endpoint": endpoint, "server_key": server_key,
                "invitation_id": invitation.id, "secret": encode(invitation.secret),
                "expires_at": invitation.expires_at}

    def _expire(self) -> None:
        now = time.monotonic()
        self.invitations = {key: value for key, value in self.invitations.items() if value.deadline > now}

    def invitation(self, invitation_id: str) -> Invitation:
        with self._mutex:
            self._expire()
            return self.invitations[invitation_id]

    def cancel(self, invitation_id: str) -> None:
        with self._mutex:
            self.invitations.pop(invitation_id, None)

    def pending_invitations(self) -> list[dict]:
        with self._mutex:
            self._expire()
            return [{"id": item.id, "label": item.label, "expires_at": item.expires_at}
                    for item in self.invitations.values()]

    def consume(self, invitation: Invitation, client_public_key: bytes) -> dict:
        with self._mutex:
            # Recheck after the handshake: expiry, cancellation and concurrent claims all win.
            if self.invitation(invitation.id) is not invitation:
                raise KeyError(invitation.id)
            public_key = encode(client_public_key)
            with self._locked():
                data = self._read()
                if len(data["peers"]) >= MAX_PEERS:
                    raise ValueError("Paired device limit reached")
                if any(peer["public_key"] == public_key for peer in data["peers"].values()):
                    raise ValueError("This device is already paired")
                peer = {"id": secrets.token_hex(16), "label": invitation.label,
                        "public_key": public_key, "created_at": time.time(),
                        "permissions": ["models.list", "chat.completions"]}
                data["peers"][peer["id"]] = peer
                self._write(data)
            del self.invitations[invitation.id]
            return peer
