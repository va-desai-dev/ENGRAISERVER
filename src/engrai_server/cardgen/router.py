"""Card Studio's HTTP surface, mounted under /cardgen.

CardGen V2 ran its own Node server with accounts, provider keys and a model
settings sheet. Inside ENGRAI none of that exists: the console session is the
login, and generation always targets whichever text and image models the
console has loaded. Nothing here reaches outside the host.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles

from ..credentials import SESSION_TTL_SECONDS, CredentialStore
from ..image_engine import ImageControlPlane, ImageEngineError
from ..llamacpp import LlamaCppControlPlane, LlamaCppError
from ..profiles import ProfileError
from .imaging import image_dimensions, requested_size
from .jobs import (
    SSE_DONE,
    Job,
    JobRegistry,
    SseContentExtractor,
    TooManyJobs,
    sse,
    sse_delta,
    sse_error,
    sse_finish,
)
from .store import LibraryStore, StoredImage, StoreError, parse_image, to_v2

WEB_DIR = Path(__file__).parent / "web"
SESSION_EXPIRED = {"X-Session-Expired": "1"}
# Lets <img src> load library images, which cannot send a header. Scoped to
# the Card Studio API, HttpOnly and SameSite=Strict, and honoured for reads
# only, so it can neither be read by script nor ride a cross-site write. The
# alternative, the session in a query string, lands in every access log.
IMAGE_COOKIE = "engrai_studio"
IMAGE_COOKIE_PATH = "/cardgen/api/"

# Sampling and formatting fields CardGen sends that reach the engine verbatim.
TEXT_PASSTHROUGH = (
    "top_p", "top_k", "min_p", "typical_p", "repetition_penalty", "frequency_penalty",
    "presence_penalty", "seed", "stop", "logit_bias", "response_format", "reasoning_effort",
    "chat_template_kwargs",
)
# Without a nucleus cap the whole vocabulary tail stays reachable, and at
# CardGen's temperatures one junk token derails everything written after it.
DEFAULT_TOP_P = 0.95
DEFAULT_EDIT_STRENGTH = 0.8
LIBRARY_KINDS = ("cards", "prompts", "playground-images", "lorebooks")
MAX_LOREBOOK_ENTRIES = 2000


class EngineNotReady(RuntimeError):
    """The console has no model of the needed kind loaded."""


def _error(status: int, message: str, **extra: Any) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, **extra}})


def _bare(image: StoredImage | str) -> str:
    if isinstance(image, StoredImage):
        return image.base64
    return image.split(",", 1)[-1] if image.startswith("data:") else image


def create_cardgen(
    *,
    credentials: CredentialStore,
    control: LlamaCppControlPlane,
    images: ImageControlPlane,
    data_dir: Path,
) -> Callable[[FastAPI], None]:
    """Build Card Studio and return the function that installs it on an app."""

    store = LibraryStore(data_dir)
    jobs = JobRegistry()
    api = APIRouter(prefix="/cardgen/api")

    # ── Session ──────────────────────────────────────────────────────────
    def bearer_session(authorization: str | None) -> str:
        scheme, _, value = (authorization or "").partition(" ")
        token = value.strip() if scheme.lower() == "bearer" else ""
        return token if token and credentials.verify_session(token) else ""

    def require_session(
        request: Request,
        authorization: str | None = Header(default=None),
        studio_cookie: str | None = Cookie(default=None, alias=IMAGE_COOKIE),
    ) -> str:
        """The console session: the bearer header, or the image cookie on reads."""
        token = bearer_session(authorization)
        if not token and request.method in ("GET", "HEAD") and studio_cookie:
            token = studio_cookie if credentials.verify_session(studio_cookie) else ""
        if token:
            return token
        raise HTTPException(401, detail="Sign in to the ENGRAI console", headers=SESSION_EXPIRED)

    session = [Depends(require_session)]

    @api.post("/session/image-cookie")
    async def image_cookie(request: Request, authorization: str | None = Header(default=None)) -> Response:
        """Exchange the bearer session for the read-only image cookie.

        Bearer only: a cookie must never be able to mint a fresh cookie.
        """
        token = bearer_session(authorization)
        if not token:
            raise HTTPException(401, detail="Sign in to the ENGRAI console", headers=SESSION_EXPIRED)
        forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
        response = Response(status_code=204)
        response.set_cookie(
            IMAGE_COOKIE,
            token,
            max_age=SESSION_TTL_SECONDS,
            path=IMAGE_COOKIE_PATH,
            httponly=True,
            samesite="strict",
            secure=(forwarded or request.url.scheme) == "https",
        )
        return response

    # ── Engine ───────────────────────────────────────────────────────────
    def loaded_text() -> str | None:
        state = control.state
        return state.active_model if state.active_model and state.phase == "ready" else None

    def loaded_image() -> str | None:
        state = images.state
        return state.active_model if state.active_model and state.phase == "ready" else None

    def describe(profiles: Any, model_id: str | None) -> dict[str, Any] | None:
        if not model_id:
            return None
        try:
            name = profiles.get(model_id).name
        except (KeyError, ProfileError):
            name = model_id
        return {"id": model_id, "name": name or model_id}

    @api.get("/engine", dependencies=session)
    async def engine() -> dict[str, Any]:
        """What Card Studio will generate with right now."""
        text_state, image_state = control.state, images.state
        return {
            "text": {
                "model": describe(control.profiles, loaded_text()),
                "phase": text_state.phase,
                "loading": describe(control.profiles, text_state.target_model),
                "busy": control.generation_lock.locked(),
            },
            "image": {
                "model": describe(images.profiles, loaded_image()),
                "phase": image_state.phase,
                "loading": describe(images.profiles, image_state.target_model),
                "busy": images.generation_lock.locked(),
            },
        }

    # ── Jobs ─────────────────────────────────────────────────────────────
    def owned_job(job_id: str) -> Job:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, detail={"error": {"code": "404", "message": "Job not found or expired"}})
        return job

    @api.get("/jobs", dependencies=session)
    async def list_jobs(clientRef: str | None = None) -> dict[str, Any]:
        return {"jobs": [job.public() for job in jobs.list(clientRef)]}

    @api.get("/jobs/{job_id}/result", dependencies=session)
    async def job_result(job_id: str) -> JSONResponse:
        job = owned_job(job_id)
        if job.status == "running":
            return JSONResponse({"status": "running", "length": len(job.content)}, status_code=202)
        if job.status == "error":
            return JSONResponse({"status": "error", "error": job.error})
        result = job.result or {
            "choices": [
                {"index": 0, "message": {"content": job.content}, "finish_reason": job.finish_reason}
            ]
        }
        return JSONResponse({"status": "done", "finishReason": job.finish_reason, "result": result})

    @api.get("/jobs/{job_id}/stream", dependencies=session)
    async def job_stream(job_id: str, request: Request, from_: int = Query(0, alias="from")) -> StreamingResponse:
        job = owned_job(job_id)
        backlog = job.content[max(0, from_):]
        # Subscribed before yielding anything, with no await in between, so a
        # chunk can neither slip through the gap nor arrive twice.
        queue = jobs.subscribe(job) if job.status == "running" else None
        if queue is not None:
            job.subscribers += 1

        async def replay() -> AsyncIterator[bytes]:
            try:
                if backlog:
                    yield sse_delta(backlog)
                if queue is None:
                    yield sse_error(job.error) if job.status == "error" and job.error else sse_finish(job.finish_reason)
                    yield SSE_DONE
                    return
                async for frame in _drain(queue, raw=False):
                    yield frame
            finally:
                if queue is not None:
                    jobs.unsubscribe(job, queue)

        return _event_stream(replay())

    async def _drain(queue: asyncio.Queue, *, raw: bool) -> AsyncIterator[bytes]:
        """Turn job events into SSE frames until the job is terminal."""
        while True:
            kind, value = await queue.get()
            if kind == "raw":
                if raw:
                    yield value
            elif kind == "delta":
                if not raw:
                    yield sse_delta(value)
            elif kind == "error":
                yield sse_error(value or "Generation failed")
                yield SSE_DONE
                return
            else:
                if not raw:
                    yield sse_finish(value)
                    yield SSE_DONE
                return

    def _event_stream(body: AsyncIterator[bytes]) -> StreamingResponse:
        return StreamingResponse(
            body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Text ─────────────────────────────────────────────────────────────
    def text_payload(body: dict[str, Any], model: str) -> bytes:
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(400, detail="messages are required")
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": body["max_tokens"] if body.get("max_tokens") is not None else 1000,
            # `is not None`, not truthiness: temperature 0 is a real request.
            "temperature": body["temperature"] if body.get("temperature") is not None else 0.7,
            "stream": bool(body.get("stream")),
        }
        for key in TEXT_PASSTHROUGH:
            if body.get(key) is not None:
                payload[key] = body[key]
        payload.setdefault("top_p", DEFAULT_TOP_P)
        return json.dumps(payload).encode("utf-8")

    async def open_text(raw: bytes) -> tuple[httpx.Response, AsyncIterator[bytes]]:
        # Authoring prompts are about a character, never about the owner.
        return await control.forward_openai(
            "POST", "chat/completions", raw, "", "application/json", ledger=False
        )

    def text_failure(exc: Exception) -> JSONResponse:
        if isinstance(exc, EngineNotReady):
            return _error(409, str(exc), code="no_text_model")
        if isinstance(exc, KeyError):
            return _error(409, "The loaded text model is no longer registered", code="no_text_model")
        return _error(502, f"Text engine failed: {exc}", type="upstream_error")

    def require_text() -> str:
        model = loaded_text()
        if model is None:
            raise EngineNotReady(
                "No text model is loaded. Load one from the ENGRAI console, then try again."
            )
        return model

    @api.post("/text/chat/completions", dependencies=session)
    async def chat_completions(request: Request) -> Response:
        body = await _json_object(request)
        stream = bool(body.get("stream"))
        try:
            raw = text_payload(body, require_text())
        except EngineNotReady as exc:
            return text_failure(exc)

        if not body.get("resumable"):
            # The plain path: a disconnect closes the stream, which releases the
            # engine for the next request instead of generating into nothing.
            try:
                response, chunks = await open_text(raw)
            except (KeyError, LlamaCppError, httpx.HTTPError) as exc:
                return text_failure(exc)
            return StreamingResponse(
                chunks,
                status_code=response.status_code,
                media_type=response.headers.get("content-type"),
            )

        try:
            job = jobs.create(body.get("clientRef"))
        except TooManyJobs as exc:
            return _error(429, str(exc), code="429")
        started: asyncio.Future[Response | None] = asyncio.get_running_loop().create_future()
        first = jobs.subscribe(job)

        async def run() -> None:
            try:
                response, chunks = await open_text(raw)
            except Exception as exc:  # noqa: BLE001 — every failure must end the job
                jobs.finish(job, "error", error=str(exc))
                if not started.done():
                    started.set_result(text_failure(exc))
                return
            if response.status_code >= 400:
                detail = b"".join([chunk async for chunk in chunks]).decode("utf-8", "replace")
                jobs.finish(job, "error", error=f"Text engine error {response.status_code}: {detail[:500]}")
                started.set_result(
                    Response(detail, status_code=response.status_code, media_type="application/json")
                )
                return
            started.set_result(None)
            try:
                if stream:
                    extractor = SseContentExtractor()
                    async for chunk in chunks:
                        jobs.raw(job, chunk)
                        for kind, value in extractor.feed(chunk):
                            if kind == "finish":
                                job.finish_reason = value
                            elif not jobs.append(job, value):
                                return  # over the buffer cap; the job says why
                else:
                    data = json.loads(b"".join([chunk async for chunk in chunks]))
                    message = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
                    job.content = message.get("content") or message.get("reasoning_content") or ""
                    job.result = data
                    job.finish_reason = ((data.get("choices") or [{}])[0] or {}).get("finish_reason")
                jobs.finish(job, "done", finish_reason=job.finish_reason or "stop")
            except asyncio.CancelledError:
                jobs.finish(job, "error", error="Generation was interrupted")
                raise
            except Exception as exc:  # noqa: BLE001
                jobs.finish(job, "error", error=f"Text engine stream failed: {exc}")
            finally:
                # Closing the stream is what releases the engine's generation
                # lock, on every exit: done, capped, failed or cancelled.
                await chunks.aclose()

        job.task = asyncio.create_task(run())
        failure = await asyncio.shield(started)
        if failure is not None:
            jobs.unsubscribe(job, first)
            return failure

        if not stream:
            try:
                await asyncio.shield(job.task)
            finally:
                jobs.unsubscribe(job, first)
            if job.status == "error":
                return _error(502, job.error or "Generation failed")
            return JSONResponse(job.result)

        async def live() -> AsyncIterator[bytes]:
            # The id goes first so the browser can resume even if the socket
            # dies on the very next chunk.
            try:
                yield sse({"type": "job", "jobId": job.id})
                async for frame in _drain(first, raw=True):
                    yield frame
            finally:
                jobs.unsubscribe(job, first)

        return _event_stream(live())

    # ── Images ───────────────────────────────────────────────────────────
    def require_image() -> str:
        model = loaded_image()
        if model is None:
            raise EngineNotReady(
                "No image model is loaded. Load one from the ENGRAI console, then try again."
            )
        return model

    def source_image(value: object, label: str) -> tuple[str, tuple[int, int] | None]:
        image = parse_image(value)
        if image is None:
            raise ImageEngineError(f"{label} must be an uploaded or generated PNG, JPEG, WebP or GIF")
        try:
            dims = image_dimensions(base64.b64decode(image.base64[:65536] + "=" * (-min(len(image.base64), 65536) % 4)))
        except (binascii.Error, ValueError):
            dims = None
        return image.base64, dims

    async def render(body: dict[str, Any], references: list[object] | None = None) -> dict[str, Any]:
        model = require_image()
        prompt = body.get("prompt")
        payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
        for key in ("seed", "negative_prompt", "steps", "cfg_scale", "sampler_name", "scheduler"):
            if body.get(key) not in (None, ""):
                payload[key] = body[key]

        init: str | None = None
        refs: list[str] = []
        fallback: tuple[int, int] | None = (1024, 1024)
        strength: float | None = None
        if references is not None:
            for index, value in enumerate(references):
                encoded, dims = source_image(value, f"Reference image {index + 1}")
                refs.append(encoded)
                if index == 0 and dims:
                    fallback = dims
        elif body.get("image"):
            init, dims = source_image(body["image"], "The source image")
            refs.append(init)
            fallback = dims or fallback
            raw_strength = body.get("strength", DEFAULT_EDIT_STRENGTH)
            if isinstance(raw_strength, bool) or not isinstance(raw_strength, (int, float)):
                raise ImageEngineError("strength must be a number")
            strength = float(raw_strength)

        width, height = requested_size(body, fallback) or (1024, 1024)
        payload["size"] = f"{width}x{height}"
        generated = await images.generate(
            payload, init_image=init, references=tuple(refs), strength=strength
        )
        data = []
        for item in generated:
            encoded = _bare(item["b64_json"])
            data.append(
                {
                    # A data: URL rather than a result link: CardGen stores,
                    # crops and embeds what it gets, and this needs no fetch.
                    "url": f"data:image/png;base64,{encoded}",
                    "b64_json": encoded,
                    "revised_prompt": str(prompt or ""),
                    "seed": item.get("seed"),
                    "model": item.get("model"),
                }
            )
        return {"created": int(time.time()), "data": data}

    def image_failure(exc: Exception) -> JSONResponse:
        if isinstance(exc, EngineNotReady):
            return _error(409, str(exc), code="no_image_model")
        if isinstance(exc, KeyError):
            return _error(409, "The loaded image model is no longer registered", code="no_image_model")
        if isinstance(exc, (ImageEngineError, ProfileError)):
            return _error(400, str(exc), type="invalid_request_error")
        if isinstance(exc, httpx.HTTPStatusError):
            return _error(502, f"Image engine error {exc.response.status_code}: {exc.response.text[:500]}")
        return _error(502, f"Image engine failed: {exc}", type="upstream_error")

    async def image_request(request: Request, *, combine: bool) -> JSONResponse:
        body = await _json_object(request)
        references = None
        if combine:
            references = body.get("input_references")
            if not isinstance(references, list) or len(references) < 2:
                return _error(400, "At least two input_references images are required")

        if not body.get("resumable"):
            try:
                return JSONResponse(await render(body, references))
            except Exception as exc:  # noqa: BLE001
                return image_failure(exc)

        try:
            job = jobs.create(body.get("clientRef"))
        except TooManyJobs as exc:
            return _error(429, str(exc), code="429")

        async def run() -> JSONResponse | None:
            try:
                job.result = await render(body, references)
                jobs.finish(job, "done", finish_reason="stop")
                return None
            except Exception as exc:  # noqa: BLE001
                failure = image_failure(exc)
                jobs.finish(job, "error", error=json.loads(failure.body)["error"]["message"])
                return failure

        # Not tied to this request: the image finishes into the job even if the
        # phone locks, and the client collects it from /jobs/{id}/result.
        job.task = asyncio.create_task(run())
        failure = await asyncio.shield(job.task)
        return failure or JSONResponse(job.result)

    @api.post("/image/generations", dependencies=session)
    async def image_generations(request: Request) -> JSONResponse:
        return await image_request(request, combine=False)

    @api.post("/image/combine", dependencies=session)
    async def image_combine(request: Request) -> JSONResponse:
        return await image_request(request, combine=True)

    # ── Library ──────────────────────────────────────────────────────────
    def serve_image(image: StoredImage | None) -> Response:
        if image is None:
            return Response(status_code=404)
        try:
            content = image.decode()
        except (binascii.Error, ValueError):
            return Response(status_code=404)
        return Response(
            content,
            media_type=image.mime,
            headers={"Cache-Control": "private, no-cache", "X-Content-Type-Options": "nosniff"},
        )

    async def all_cards() -> list[dict[str, Any]]:
        # CardGen's older history file stays visible; it is never migrated or
        # deleted implicitly.
        return [*await store.read("cards"), *await store.read("history")]

    def library_kind(kind: str) -> str:
        if kind not in LIBRARY_KINDS:
            raise HTTPException(404, detail="Not found")
        return kind

    @api.get("/config", dependencies=session)
    async def get_config() -> Any:
        return await store.read("config", {})

    @api.post("/config", dependencies=session)
    async def put_config(request: Request) -> dict[str, bool]:
        body = await _json_object(request)
        await store.update("config", lambda _: body, {})
        return {"success": True}

    @api.get("/storage/cards/thumbnail", dependencies=session)
    async def card_thumbnail(cardId: str = "") -> Response:
        card = next((c for c in await all_cards() if str(c.get("id")) == cardId), None)
        if card and card.get("imageBase64"):
            return serve_image(parse_image(card["imageBase64"]))
        return serve_image(store.legacy_image(cardId))

    @api.get("/storage/playground-images/{image_id}/image", dependencies=session)
    async def playground_image(image_id: str) -> Response:
        row = next((r for r in await store.read("playground-images") if r.get("id") == image_id), None)
        return serve_image(parse_image(row.get("imageBase64")) if row else None)

    def gallery_kind(card_id: str) -> str:
        if not card_id or not all(ch.isalnum() or ch in "_-" for ch in card_id):
            raise HTTPException(400, detail="Invalid card ID")
        return f"gallery-{card_id}"

    def gallery_row(row: dict[str, Any], card_id: str) -> dict[str, Any]:
        return {
            "id": row["id"],
            "order": row.get("order"),
            "url": f"/cardgen/api/storage/cards/{card_id}/gallery/{row['id']}/image",
        }

    @api.get("/storage/cards/{card_id}/gallery", dependencies=session)
    async def gallery(card_id: str) -> list[dict[str, Any]]:
        return [gallery_row(r, card_id) for r in await store.read(gallery_kind(card_id))]

    @api.post("/storage/cards/{card_id}/gallery", dependencies=session)
    async def add_gallery(card_id: str, request: Request) -> Any:
        kind = gallery_kind(card_id)
        body = await _json_object(request)
        if not any(str(c.get("id")) == card_id for c in await all_cards()):
            return JSONResponse({"error": "Save the card first"}, status_code=404)
        if parse_image(body.get("imageBase64")) is None:
            return JSONResponse({"error": "Invalid image"}, status_code=400)
        saved = await store.save(kind, {"imageBase64": body["imageBase64"]})
        return gallery_row(saved, card_id)

    @api.put("/storage/cards/{card_id}/gallery/reorder", dependencies=session)
    async def reorder_gallery(card_id: str, request: Request) -> Any:
        body = await _json_object(request)
        ordered = body.get("orderedIds")
        if not isinstance(ordered, list):
            return JSONResponse({"error": "Expected orderedIds"}, status_code=400)

        def change(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            by_id = {r["id"]: r for r in rows}
            if len(ordered) != len(rows) or set(ordered) != set(by_id):
                raise StoreError("Invalid gallery order")
            return [{**by_id[i], "order": n} for n, i in enumerate(ordered)]

        try:
            await store.update(gallery_kind(card_id), change)
        except StoreError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"success": True}

    @api.get("/storage/cards/{card_id}/gallery/{image_id}/image", dependencies=session)
    async def gallery_image(card_id: str, image_id: str) -> Response:
        row = next((r for r in await store.read(gallery_kind(card_id)) if r.get("id") == image_id), None)
        return serve_image(parse_image(row.get("imageBase64")) if row else None)

    @api.delete("/storage/cards/{card_id}/gallery/{image_id}", dependencies=session)
    async def delete_gallery_image(card_id: str, image_id: str) -> dict[str, bool]:
        await store.remove(gallery_kind(card_id), image_id)
        return {"success": True}

    @api.get("/storage/{kind}", dependencies=session)
    async def list_records(kind: str) -> list[dict[str, Any]]:
        kind = library_kind(kind)
        rows = await all_cards() if kind == "cards" else await store.read(kind)
        listed = []
        for row in rows:
            summary = {k: v for k, v in row.items() if k not in ("imageBase64", "imageHistory")}
            if kind == "playground-images":
                summary["url"] = f"/cardgen/api/storage/{kind}/{row['id']}/image"
            listed.append(summary)
        return listed

    @api.post("/storage/{kind}", dependencies=session)
    async def save_record(kind: str, request: Request) -> Any:
        kind = library_kind(kind)
        record = await _json_object(request)
        if kind == "cards":
            try:
                card = to_v2(record.get("character"))
            except StoreError as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
            record["card"] = card
            record["characterName"] = card["data"]["name"]
            record["isPermanent"] = record.get("isPermanent") is not False
        if kind == "lorebooks":
            # A world lorebook stands alone: a name, the setting it describes
            # and its entries. Checked here so one bad save cannot leave the
            # library holding something the tab cannot open again.
            entries = record.get("entries")
            if not str(record.get("name") or "").strip():
                return JSONResponse({"error": "Give the lorebook a name before saving."}, status_code=400)
            if not isinstance(entries, list) or len(entries) > MAX_LOREBOOK_ENTRIES or not all(
                isinstance(e, dict) and isinstance(e.get("keys"), list) and isinstance(e.get("content"), str)
                for e in entries
            ):
                return JSONResponse({"error": "Lorebook entries must each have keys and content."}, status_code=400)
        if record.get("imageBase64") and parse_image(record["imageBase64"]) is None:
            return JSONResponse({"error": "Invalid image"}, status_code=400)
        history = record.get("imageHistory")
        if isinstance(history, list) and any(parse_image(item) is None for item in history):
            return JSONResponse({"error": "Invalid image history"}, status_code=400)
        saved = await store.save(kind, record)
        if kind == "playground-images":
            return {**saved, "url": f"/cardgen/api/storage/{kind}/{saved['id']}/image"}
        return saved

    @api.get("/storage/{kind}/{record_id}", dependencies=session)
    async def get_record(kind: str, record_id: str) -> Any:
        kind = library_kind(kind)
        rows = await all_cards() if kind == "cards" else await store.read(kind)
        row = next((r for r in rows if str(r.get("id")) == record_id), None)
        if row is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return row

    @api.delete("/storage/{kind}/{record_id}", dependencies=session)
    async def delete_record(kind: str, record_id: str) -> dict[str, bool]:
        kind = library_kind(kind)
        await store.remove(kind, record_id)
        if kind == "cards":
            await store.remove("history", record_id)
        return {"success": True}

    # ── Page ─────────────────────────────────────────────────────────────
    def install(app: FastAPI) -> None:
        @app.exception_handler(StoreError)
        async def store_failure(_: Request, exc: StoreError) -> JSONResponse:
            return JSONResponse({"error": str(exc)}, status_code=500)

        app.include_router(api)

        @app.api_route("/cardgen", methods=["GET", "HEAD"], include_in_schema=False)
        async def cardgen_redirect() -> RedirectResponse:
            return RedirectResponse("/cardgen/", status_code=308)

        @app.api_route("/cardgen/", methods=["GET", "HEAD"], include_in_schema=False)
        async def cardgen_index() -> HTMLResponse:
            # Unauthenticated like the console shell: the page holds no data
            # and sends the visitor to sign in when there is no session.
            return HTMLResponse(
                (WEB_DIR / "index.html").read_text(encoding="utf-8"),
                headers={"Cache-Control": "no-cache, must-revalidate"},
            )

        app.mount("/cardgen", RevalidatedStaticFiles(directory=WEB_DIR), name="cardgen")

    return install


class RevalidatedStaticFiles(StaticFiles):
    """Unhashed script names, so every load revalidates against the ETag."""

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, detail="Request body is not valid JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, detail="Request body must be an object")
    return body
