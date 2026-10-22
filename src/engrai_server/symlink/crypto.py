"""The versioned SymLink wire contract, backed by Noise (no custom crypto)."""

from __future__ import annotations

import base64
import json

from noise.connection import Keypair, NoiseConnection

PAIR_PROTOCOL = b"Noise_XXpsk0_25519_ChaChaPoly_SHA256"
SESSION_PROTOCOL = b"Noise_IK_25519_ChaChaPoly_SHA256"
MAX_FRAME = 65535
MAX_PLAINTEXT = MAX_FRAME - 16
SUBPROTOCOL = "engrai.symlink.v1"


def encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def decode_key(value: str) -> bytes:
    if not isinstance(value, str) or len(value) != 43:
        raise ValueError("Expected a base64url 32-byte key")
    raw = base64.b64decode(value + "=", altchars=b"-_", validate=True)
    if len(raw) != 32 or encode(raw) != value:
        raise ValueError("Invalid key encoding")
    return raw


def prologue(mode: str, identifier: str, server_key: str) -> bytes:
    return f"ENGRAI-SymLink/1\n{mode}\n{identifier}\n{server_key}".encode("ascii")


def responder(private_key: bytes, *, mode: str, identifier: str, server_key: str,
              psk: bytes | None = None) -> NoiseConnection:
    connection = NoiseConnection.from_name(PAIR_PROTOCOL if mode == "pair" else SESSION_PROTOCOL)
    connection.set_as_responder()
    connection.set_keypair_from_private_bytes(Keypair.STATIC, private_key)
    connection.set_prologue(prologue(mode, identifier, server_key))
    if psk is not None:
        connection.set_psks(psk=psk)
    connection.start_handshake()
    return connection


def pack(connection: NoiseConnection, payload: dict) -> bytes:
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_PLAINTEXT:
        raise ValueError("SymLink message too large")
    return connection.encrypt(data)


def unpack(connection: NoiseConnection, frame: bytes) -> dict:
    if not 16 <= len(frame) <= MAX_FRAME:
        raise ValueError("Invalid SymLink frame length")
    payload = json.loads(connection.decrypt(frame))
    if not isinstance(payload, dict):
        raise ValueError("Expected a SymLink object")
    return payload
