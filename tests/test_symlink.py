"""Real Noise initiator/responder tests across the ASGI WebSocket boundary."""

from __future__ import annotations

import asyncio
import base64
import json
import stat
import threading
import warnings
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from noise.connection import Keypair, NoiseConnection
from starlette.websockets import WebSocketDisconnect

from engrai_server.symlink import PairingStore, create_symlink
from engrai_server.symlink.crypto import PAIR_PROTOCOL, SESSION_PROTOCOL, SUBPROTOCOL, decode_key, encode, prologue
from engrai_server.symlink.store import endpoint_url

ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def service(tmp_path):
    store = PairingStore(tmp_path / "symlink.json")
    app = FastAPI()
    calls = []
    stopped = threading.Event()

    def admin(authorization: str | None = Header(default=None)):
        if authorization != ADMIN["Authorization"]:
            raise HTTPException(401)

    async def models():
        return {"object": "list", "data": [{"id": "test-model"}]}

    async def chat(body):
        calls.append(body)

        async def stream():
            try:
                yield b'data: {"text":"private text"}\n\n'
                if body.get("wait"):
                    await asyncio.sleep(30)
                yield b"data: [DONE]\n\n"
            finally:
                stopped.set()

        return StreamingResponse(stream(), media_type="text/event-stream")

    app.include_router(create_symlink(store=store, require_admin=admin, models=models, chat=chat))
    with TestClient(app) as client:
        yield client, store, calls, stopped


def invitation(client):
    response = client.post("/control/symlink/invitations", headers=ADMIN,
                           json={"label": "My iPhone", "origin": "https://host.example"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    value = response.json()
    assert value["qr"].startswith("data:image/svg+xml;base64,")
    encoded = value["uri"].split("#", 1)[1]
    return json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))


def initiator(invite, private, *, peer_id=None, psk=None):
    connection = NoiseConnection.from_name(SESSION_PROTOCOL if peer_id else PAIR_PROTOCOL)
    connection.set_as_initiator()
    connection.set_keypair_from_private_bytes(Keypair.STATIC, private)
    connection.set_prologue(prologue("session" if peer_id else "pair",
                                     peer_id or invite["invitation_id"], invite["server_key"]))
    if peer_id:
        connection.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, decode_key(invite["server_key"]))
    else:
        connection.set_psks(psk=psk or decode_key(invite["secret"]))
    connection.start_handshake()
    return connection


def read(ws, connection):
    frame = ws.receive_bytes()
    assert b"private text" not in frame
    return json.loads(connection.decrypt(frame))


def write(ws, connection, payload):
    frame = connection.encrypt(json.dumps(payload).encode())
    ws.send_bytes(frame)
    return frame


def finish_pair(ws, connection, invite):
    ws.send_bytes(bytes(connection.write_message()))
    assert not connection.read_message(ws.receive_bytes())
    assert encode(connection.noise_protocol.handshake_state.rs.public_bytes) == invite["server_key"]
    ws.send_bytes(bytes(connection.write_message()))
    return read(ws, connection)


@contextmanager
def paired(client):
    invite = invitation(client)
    private = X25519PrivateKey.generate().private_bytes_raw()
    connection = initiator(invite, private)
    with client.websocket_connect(f'/symlink/v1/pair/{invite["invitation_id"]}', subprotocols=[SUBPROTOCOL]) as ws:
        ready = finish_pair(ws, connection, invite)
        yield ws, connection, invite, private, ready["peer_id"]


def response_body(ws, connection, identifier="one"):
    start = read(ws, connection)
    assert start["type"] == "response.start"
    body = bytearray()
    while True:
        message = read(ws, connection)
        assert message["id"] == identifier
        if message["type"] == "response.end":
            return start, bytes(body)
        assert message["type"] == "response.chunk"
        body.extend(base64.b64decode(message["data"]))


def test_admin_required_for_pairing_and_management(service):
    client, store, *_ = service
    assert client.get("/control/symlink").status_code == 401
    assert client.post("/control/symlink/invitations", json={"label": "x", "origin": "https://x"}).status_code == 401
    assert client.delete("/control/symlink/peers/unknown").status_code == 401
    assert client.delete("/control/symlink/invitations/unknown").status_code == 401
    assert not store.path.exists()


def test_pair_stream_and_reconnect_after_store_reopen(service):
    client, store, calls, _ = service
    with paired(client) as (ws, connection, invite, private, peer_id):
        write(ws, connection, {"type": "request", "id": "one", "operation": "models.list"})
        start, body = response_body(ws, connection)
        assert start["status"] == 200
        assert json.loads(body)["data"][0]["id"] == "test-model"
        write(ws, connection, {"type": "request", "id": "two", "operation": "chat.completions",
                               "body": {"model": "test-model", "stream": True}})
        start, body = response_body(ws, connection, "two")
        assert start["content_type"].startswith("text/event-stream")
        assert body == b'data: {"text":"private text"}\n\ndata: [DONE]\n\n'
        assert len(calls) == 1
    reopened = PairingStore(store.path)
    assert reopened.identity() == store.identity()
    assert reopened.peer(peer_id)["label"] == "My iPhone"
    assert not reopened.pending_invitations()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert invite["secret"] not in store.path.read_text()
    connection = initiator(invite, private, peer_id=peer_id)
    with client.websocket_connect(f"/symlink/v1/session/{peer_id}", subprotocols=[SUBPROTOCOL]) as ws:
        ws.send_bytes(bytes(connection.write_message()))
        assert not connection.read_message(ws.receive_bytes())
        assert read(ws, connection)["peer_id"] == peer_id


def test_wrong_pairing_secret_does_not_consume_invitation(service):
    client, store, *_ = service
    invite = invitation(client)
    private = X25519PrivateKey.generate().private_bytes_raw()
    connection = initiator(invite, private, psk=b"x" * 32)
    with client.websocket_connect(f'/symlink/v1/pair/{invite["invitation_id"]}', subprotocols=[SUBPROTOCOL]) as ws:
        ws.send_bytes(bytes(connection.write_message()))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert len(store.pending_invitations()) == 1
    assert not store.peers()


def test_invitation_can_only_be_claimed_once_even_with_concurrent_handshakes(service):
    client, store, *_ = service
    invite = invitation(client)
    path = f'/symlink/v1/pair/{invite["invitation_id"]}'
    a = initiator(invite, X25519PrivateKey.generate().private_bytes_raw())
    b = initiator(invite, X25519PrivateKey.generate().private_bytes_raw())
    with client.websocket_connect(path, subprotocols=[SUBPROTOCOL]) as ws1:
        with client.websocket_connect(path, subprotocols=[SUBPROTOCOL]) as ws2:
            for ws, connection in [(ws1, a), (ws2, b)]:
                ws.send_bytes(bytes(connection.write_message()))
                connection.read_message(ws.receive_bytes())
            ws1.send_bytes(bytes(a.write_message()))
            assert read(ws1, a)["type"] == "ready"
            ws2.send_bytes(bytes(b.write_message()))
            with pytest.raises(WebSocketDisconnect):
                ws2.receive_bytes()
    assert len(store.peers()) == 1


@pytest.mark.parametrize("change", ["cancel", "expire"])
def test_invitation_rechecked_at_end_of_handshake(service, change):
    client, store, *_ = service
    invite = invitation(client)
    connection = initiator(invite, X25519PrivateKey.generate().private_bytes_raw())
    with client.websocket_connect(f'/symlink/v1/pair/{invite["invitation_id"]}', subprotocols=[SUBPROTOCOL]) as ws:
        ws.send_bytes(bytes(connection.write_message()))
        connection.read_message(ws.receive_bytes())
        if change == "cancel":
            assert client.delete(f'/control/symlink/invitations/{invite["invitation_id"]}', headers=ADMIN).status_code == 200
        else:
            store.invitation(invite["invitation_id"]).deadline = 0
        ws.send_bytes(bytes(connection.write_message()))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert not store.peers()


@pytest.mark.parametrize("attack", ["replay", "tamper", "plaintext", "oversize", "duplicate_id"])
def test_invalid_records_close_channel(service, attack):
    client, *_ = service
    with paired(client) as (ws, connection, *_):
        payload = {"type": "request", "id": "one", "operation": "ping"}
        original = write(ws, connection, payload)
        response_body(ws, connection)
        if attack == "replay":
            ws.send_bytes(original)
        elif attack == "tamper":
            frame = connection.encrypt(json.dumps({**payload, "id": "two"}).encode())
            ws.send_bytes(frame[:-1] + bytes([frame[-1] ^ 1]))
        elif attack == "plaintext":
            ws.send_text(json.dumps(payload))
        elif attack == "oversize":
            ws.send_bytes(b"x" * 65536)
        else:
            write(ws, connection, payload)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()


def test_peer_identity_required_on_reconnect(service):
    client, *_ = service
    with paired(client) as (_, _, invite, _, peer_id):
        pass
    connection = initiator(invite, X25519PrivateKey.generate().private_bytes_raw(), peer_id=peer_id)
    with client.websocket_connect(f"/symlink/v1/session/{peer_id}", subprotocols=[SUBPROTOCOL]) as ws:
        ws.send_bytes(bytes(connection.write_message()))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()


def test_revocation_cancels_active_generation_and_blocks_reconnect(service):
    client, store, _, stopped = service
    with paired(client) as (ws, connection, invite, private, peer_id):
        write(ws, connection, {"type": "request", "id": "one", "operation": "chat.completions", "body": {"wait": True}})
        assert read(ws, connection)["type"] == "response.start"
        assert read(ws, connection)["type"] == "response.chunk"
        # A second store models revocation from another process, too.
        PairingStore(store.path).revoke(peer_id)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
        assert stopped.wait(2)
    connection = initiator(invite, private, peer_id=peer_id)
    with client.websocket_connect(f"/symlink/v1/session/{peer_id}", subprotocols=[SUBPROTOCOL]) as ws:
        ws.send_bytes(bytes(connection.write_message()))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()


def test_disconnect_cancels_generation(service):
    client, _, _, stopped = service
    with paired(client) as (ws, connection, *_):
        write(ws, connection, {"type": "request", "id": "one", "operation": "chat.completions", "body": {"wait": True}})
        read(ws, connection)
        read(ws, connection)
    assert stopped.wait(2)


@pytest.mark.parametrize("operation", ["control.state", "credentials", "http.request", "images.generate"])
def test_paired_device_has_no_admin_or_arbitrary_proxy_access(service, operation):
    client, _, calls, _ = service
    with paired(client) as (ws, connection, *_):
        write(ws, connection, {"type": "request", "id": "one", "operation": operation})
        start, _ = response_body(ws, connection)
        assert start["status"] == 403
    assert not calls


def test_corrupt_identity_is_not_regenerated(tmp_path):
    path = tmp_path / "symlink.json"
    path.write_text('{"version": 9}')
    with pytest.raises(ValueError):
        PairingStore(path).identity()
    assert path.read_text() == '{"version": 9}'


@pytest.mark.parametrize("origin", ["file:///tmp/x", "https://user:pass@host", "https://host/path",
                                    "https://host?key=secret", "https://host#secret", "https://host:99999"])
def test_pairing_origins_are_validated(origin):
    with pytest.raises(ValueError):
        endpoint_url(origin)


def test_pairing_origin_is_not_fetched_and_ipv6_is_supported():
    assert endpoint_url("http://[fd00::1]:8400") == "ws://[fd00::1]:8400/symlink/v1"


def test_subprotocol_is_required(service):
    client, *_ = service
    invite = invitation(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f'/symlink/v1/pair/{invite["invitation_id"]}'):
            pass


def test_actual_gateway_routes_models_and_chat_without_api_key(tmp_path, monkeypatch):
    import importlib

    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))
    monkeypatch.setenv("ENGRAI_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "test-admin-password")
    monkeypatch.setenv("CONTROL_API_KEYS", "ordinary-api-key")
    from engrai_server.settings import get_settings
    get_settings.cache_clear()
    import engrai_server.main
    gateway = importlib.reload(engrai_server.main)
    seen = []

    async def forward(method, path, body, query, content_type):
        import httpx
        seen.append((method, path, json.loads(body), query, content_type))

        async def chunks():
            yield b'data: {"text":"private text"}\n\n'
            yield b'data: [DONE]\n\n'

        return httpx.Response(200, headers={"content-type": "text/event-stream"}), chunks()

    monkeypatch.setattr(gateway.control, "forward_openai", forward)
    try:
        with TestClient(gateway.app) as client:
            session = client.post("/control/session", json={"password": "test-admin-password"}).json()
            client.headers["Authorization"] = f'Bearer {session["token"]}'
            result = client.post("/control/symlink/invitations", json={"label": "Native client", "origin": "https://host.example"})
            encoded = result.json()["uri"].split("#")[1]
            invite = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            client.headers.clear()
            connection = initiator(invite, X25519PrivateKey.generate().private_bytes_raw())
            with client.websocket_connect(f'/symlink/v1/pair/{invite["invitation_id"]}', subprotocols=[SUBPROTOCOL]) as ws:
                finish_pair(ws, connection, invite)
                write(ws, connection, {"type": "request", "id": "one", "operation": "chat.completions",
                                       "body": {"model": "test", "messages": [{"role": "user", "content": "hello"}], "stream": True}})
                start, body = response_body(ws, connection)
                assert start["status"] == 200
                assert b"[DONE]" in body
            assert seen[0][0:2] == ("POST", "chat/completions")
            assert seen[0][2]["messages"][0]["content"] == "hello"
            assert client.get("/v1/models").status_code == 401
            assert client.get("/control/state").status_code == 401
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("mode", ["pair", "session"])
def test_native_client_interoperability_vectors(mode):
    vectors = json.loads((Path(__file__).parents[1] / "docs/symlink-v1-vectors.json").read_text())
    vector = next(item for item in vectors["vectors"] if item["mode"] == mode)
    assert vector["prologue"].encode() == prologue(mode, vector["identifier"], vector["server_key"])
    peers = {}
    for role in ("client", "server"):
        connection = NoiseConnection.from_name(vector["protocol"].encode())
        connection.set_as_initiator() if role == "client" else connection.set_as_responder()
        connection.set_keypair_from_private_bytes(Keypair.STATIC, bytes.fromhex(vector[f"{role}_private_hex"]))
        connection.set_keypair_from_private_bytes(Keypair.EPHEMERAL, bytes.fromhex(vector[f"{role}_ephemeral_hex"]))
        connection.set_prologue(vector["prologue"].encode())
        if mode == "pair":
            connection.set_psks(psk=bytes.fromhex(vector["psk_hex"]))
        elif role == "client":
            connection.set_keypair_from_public_bytes(Keypair.REMOTE_STATIC, decode_key(vector["server_key"]))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            connection.start_handshake()
        peers[role] = connection
    for message in vector["handshake"]:
        sender = peers[message["sender"]]
        receiver = peers["server" if message["sender"] == "client" else "client"]
        ciphertext = bytes(sender.write_message())
        assert ciphertext.hex() == message["hex"]
        assert receiver.read_message(ciphertext) == b""
    for connection in peers.values():
        assert connection.get_handshake_hash().hex() == vector["handshake_hash_hex"]
    request = peers["client"].encrypt(vector["request_utf8"].encode())
    assert request.hex() == vector["request_ciphertext_hex"]
    assert peers["server"].decrypt(request).decode() == vector["request_utf8"]
    response = peers["server"].encrypt(vector["response_utf8"].encode())
    assert response.hex() == vector["response_ciphertext_hex"]
    assert peers["client"].decrypt(response).decode() == vector["response_utf8"]
