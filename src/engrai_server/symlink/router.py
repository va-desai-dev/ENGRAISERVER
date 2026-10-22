"""Admin-owned pairing and end-to-end encrypted, bounded WebSocket sessions."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import re
import secrets
import time
from collections.abc import Awaitable, Callable

import qrcode
import qrcode.image.svg
from cryptography.exceptions import InvalidTag
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from noise.exceptions import NoiseInvalidMessage, NoiseValueError
from pydantic import BaseModel, ConfigDict, Field

from .crypto import MAX_FRAME, SUBPROTOCOL, decode_key, encode, pack, responder, unpack
from .store import PairingStore

HANDSHAKE_TIMEOUT = 10
IDLE_TIMEOUT = 120
REQUEST_TIMEOUT = 300
MAX_CONNECTIONS = 32
MAX_REQUESTS = 1024
IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")


class InviteBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    origin: str = Field(min_length=1, max_length=512)


async def receive_frame(ws: WebSocket) -> bytes:
    message = await ws.receive()
    if message["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(message.get("code", 1000))
    data = message.get("bytes")
    if data is None or not 16 <= len(data) <= MAX_FRAME:
        raise ValueError("Expected a bounded binary frame")
    return data


def create_symlink(*, store: PairingStore, require_admin: Callable,
                   models: Callable[[], Awaitable[dict]],
                   chat: Callable[[dict], Awaitable[Response]]) -> APIRouter:
    api = APIRouter()
    connections = 0

    @api.get("/control/symlink", dependencies=[Depends(require_admin)])
    async def state():
        _, server_key = store.identity()
        return JSONResponse({"version": 1, "server_key": server_key, "peers": store.peers(),
                             "invitations": store.pending_invitations()},
                            headers={"Cache-Control": "no-store"})

    @api.post("/control/symlink/invitations", dependencies=[Depends(require_admin)])
    async def invite(body: InviteBody):
        try:
            invitation = store.invite(body.label, body.origin)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # The fragment keeps the secret out of URL paths, queries and access logs.
        uri = "engrai://pair#" + encode(json.dumps(invitation, separators=(",", ":")).encode())
        qr = qrcode.make(uri, image_factory=qrcode.image.svg.SvgPathImage)
        svg = qr.to_string().decode("utf-8")
        return JSONResponse({"invitation_id": invitation["invitation_id"],
                             "expires_at": invitation["expires_at"], "uri": uri,
                             "qr": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()},
                            headers={"Cache-Control": "no-store"})

    @api.delete("/control/symlink/invitations/{invitation_id}", dependencies=[Depends(require_admin)])
    async def cancel_invite(invitation_id: str):
        store.cancel(invitation_id)
        return {"ok": True}

    @api.delete("/control/symlink/peers/{peer_id}", dependencies=[Depends(require_admin)])
    async def revoke_peer(peer_id: str):
        try:
            store.revoke(peer_id)
        except KeyError as exc:
            raise HTTPException(404, "Unknown paired device") from exc
        return {"peers": store.peers()}

    async def serve(ws: WebSocket, connection, peer: dict):
        store.peer(peer["id"])
        await ws.send_bytes(pack(connection, {"type": "ready", "version": 1,
                                             "peer_id": peer["id"],
                                             "permissions": peer["permissions"],
                                             "max_frame": MAX_FRAME}))
        pending: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
        seen: set[str] = set()
        last_activity = time.monotonic()
        processing = False

        async def reader():
            nonlocal last_activity
            while True:
                frame = await receive_frame(ws)
                message = unpack(connection, frame)
                last_activity = time.monotonic()
                identifier = message.get("id")
                if (message.get("type") != "request" or not isinstance(identifier, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", identifier)
                        or identifier in seen or len(seen) >= MAX_REQUESTS):
                    raise ValueError("Invalid or reused request ID")
                seen.add(identifier)
                pending.put_nowait(message)

        async def send(message: dict):
            nonlocal last_activity
            store.peer(peer["id"])  # Revocation applies to every outbound record.
            await ws.send_bytes(pack(connection, message))
            last_activity = time.monotonic()

        async def respond(message: dict):
            identifier = message["id"]
            operation = message.get("operation")
            current = store.peer(peer["id"])
            if operation == "ping":
                response = JSONResponse({"ok": True})
            elif not isinstance(operation, str) or operation not in current["permissions"]:
                response = JSONResponse({"error": "Operation not permitted"}, status_code=403)
            elif operation == "models.list":
                response = JSONResponse(await models())
            elif operation == "chat.completions" and isinstance(message.get("body"), dict):
                response = await chat(message["body"])
            else:
                response = JSONResponse({"error": "Invalid operation body"}, status_code=400)
            try:
                # Start the upstream iterator before any socket write can fail.
                # Its finally block owns releasing the inference lane; aclose()
                # alone cannot enter the finally of an unstarted async generator.
                first = None
                if isinstance(response, StreamingResponse):
                    first = await anext(response.body_iterator, None)
                await send({"type": "response.start", "id": identifier, "status": response.status_code,
                            "content_type": response.headers.get("content-type", "application/octet-stream")})

                async def chunk(data: bytes):
                    for offset in range(0, len(data), 32768):
                        await send({"type": "response.chunk", "id": identifier,
                                    "data": base64.b64encode(data[offset:offset + 32768]).decode()})

                if isinstance(response, StreamingResponse):
                    if first is not None:
                        await chunk(first.encode("utf-8") if isinstance(first, str) else bytes(first))
                    async for data in response.body_iterator:
                        await chunk(data.encode("utf-8") if isinstance(data, str) else bytes(data))
                else:
                    await chunk(bytes(response.body))
                await send({"type": "response.end", "id": identifier})
            finally:
                if isinstance(response, StreamingResponse):
                    close = getattr(response.body_iterator, "aclose", None)
                    if close is not None:
                        await close()
                if response.background is not None:
                    await response.background()

        async def worker():
            nonlocal processing, last_activity
            while True:
                message = await pending.get()
                processing = True
                try:
                    async with asyncio.timeout(REQUEST_TIMEOUT):
                        await respond(message)
                finally:
                    processing = False
                    last_activity = time.monotonic()

        async def watch_revocation():
            while True:
                await asyncio.sleep(0.25)
                store.peer(peer["id"])
                if not processing and time.monotonic() - last_activity > IDLE_TIMEOUT:
                    raise TimeoutError("Idle session")

        tasks = [asyncio.create_task(fn()) for fn in (reader, worker, watch_revocation)]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def connect(ws: WebSocket, mode: str, identifier: str):
        nonlocal connections
        if (not IDENTIFIER.fullmatch(identifier) or connections >= MAX_CONNECTIONS
                or SUBPROTOCOL not in ws.scope.get("subprotocols", [])):
            await ws.close(code=1008)
            return
        connections += 1
        try:
            await ws.accept(subprotocol=SUBPROTOCOL)
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                private, server_key = store.identity()
                invitation = store.invitation(identifier) if mode == "pair" else None
                peer = store.peer(identifier) if mode == "session" else None
                connection = responder(private, mode=mode, identifier=identifier,
                                       server_key=server_key, psk=invitation.secret if invitation else None)
                # noiseprotocol 0.3.1 discards its handshake state on completion.
                # Retain the state only until we extract the authenticated remote static key.
                handshake = connection.noise_protocol.handshake_state
                if connection.read_message(await receive_frame(ws)):
                    raise ValueError("Handshake payloads must be empty")
                if peer is not None and not secrets.compare_digest(handshake.rs.public_bytes, decode_key(peer["public_key"])):
                    raise ValueError("Peer key mismatch")
                await ws.send_bytes(bytes(connection.write_message()))
                if invitation is not None:
                    if connection.read_message(await receive_frame(ws)):
                        raise ValueError("Handshake payloads must be empty")
                    peer = store.consume(invitation, handshake.rs.public_bytes)
                    invitation = None
                del handshake
            await serve(ws, connection, peer)
        except WebSocketDisconnect:
            pass
        except (ValueError, KeyError, RecursionError, NoiseInvalidMessage, NoiseValueError, InvalidTag,
                TimeoutError, asyncio.QueueFull):
            # Never reflect handshake details, secrets, engine errors or traceback text.
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.close(code=1008, reason="SymLink session rejected or expired")
        finally:
            connections -= 1

    @api.websocket("/symlink/v1/pair/{invitation_id}")
    async def pair(ws: WebSocket, invitation_id: str):
        await connect(ws, "pair", invitation_id)

    @api.websocket("/symlink/v1/session/{peer_id}")
    async def session(ws: WebSocket, peer_id: str):
        await connect(ws, "session", peer_id)

    return api
