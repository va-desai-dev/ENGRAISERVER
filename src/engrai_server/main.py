from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from huggingface_hub.utils import HfHubHTTPError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import preflight
from .auth import build_dependencies
from .credentials import CredentialError, CredentialStore
from .domain.deployments import DeploymentProfile
from .domain.store import DeploymentStore
from .engines.llamacpp import validate_extra_arguments
from .events import EventLog
from .image_engine import ImageControlPlane, ImageEngineError
from .image_profiles import ImageProfileStore
from .identity import PRODUCT_NAME
from .llamacpp import LlamaCppControlPlane, LlamaCppError
from .metrics import MetricsCollector
from .model_browser import ModelBrowser
from .model_downloads import (
    DownloadBusyError,
    DownloadError,
    ModelDownloadManager,
    ModelPullRequest,
)
from .preferences import PreferenceError, PreferenceStore
from .profiles import RouteProfile, ProfileError, ProfileStore
from .settings import get_settings
from .throttle import LoginThrottle


settings = get_settings()
credentials = CredentialStore(
    settings.credentials_path,
    seed_api_keys=settings.control_api_keys,
    seed_admin_token=settings.control_admin_token,
)
require_api_key, require_admin, require_display = build_dependencies(credentials)
login_throttle = LoginThrottle()
metrics = MetricsCollector()
model_browser = ModelBrowser()
events = EventLog()
preferences = PreferenceStore(
    settings.preferences_path,
    default_roots=settings.search_roots,
    required_roots=[settings.model_download_dir],
)
profiles = ProfileStore(settings.route_dir, preferences.search_roots)
deployments = DeploymentStore(settings.deployment_dir)
control = LlamaCppControlPlane(settings, profiles, deployments)
image_profiles = ImageProfileStore(
    settings.image_command_dir, preferences.search_roots
)
images = ImageControlPlane(settings, image_profiles)
model_downloads = ModelDownloadManager(
    settings.model_download_dir,
    settings.model_download_state_path,
)
static_dir = Path(__file__).parent / "static"


async def gateway_snapshot() -> dict[str, Any]:
    text, image = await asyncio.gather(control.snapshot(), images.snapshot())
    return {**text, **image}


@asynccontextmanager
async def lifespan(_: FastAPI):
    # The first entry in every log, and the marker a client sees alongside a
    # new epoch when the gateway has been restarted underneath it.
    events.record("gateway.started", "Gateway started")
    yield
    await asyncio.gather(control.close(), images.close(), model_downloads.close())


app = FastAPI(
    title=PRODUCT_NAME,
    description="Sovereign host control plane and runtime orchestrator",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)

# Native Apple clients do not enforce browser CORS, but small HTML wrappers,
# local files and data: URLs do. No ambient cookies are used anywhere in this
# app, so allowing origins does not grant access: every inference and control
# request still needs its explicit bearer credential.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
    expose_headers=["Retry-After"],
    max_age=86400,
)


class ImmutableStaticFiles(StaticFiles):
    """Serve the hashed bundle as immutable.

    Vite writes a content hash into every filename under assets/, so a given
    URL can never refer to different bytes. Caching them for a year removes a
    conditional request per asset from every load, which is the difference
    between a fast and a sluggish start over a tunnel. index.html is the
    mutable pointer at these names and is served separately, uncached.
    """

    def file_response(self, *args: Any, **kwargs: Any):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


app.mount("/assets", ImmutableStaticFiles(directory=static_dir / "assets"), name="assets")


# Every GET route reachable without credentials also answers HEAD. RFC 9110
# says a server supporting GET should support HEAD, and uptime monitors lean
# on it: a HEAD probe checks liveness without pulling the whole bundle down
# on every interval. FastAPI's @app.get registers GET alone, so a probe that
# would otherwise be cheap came back 405 and read as an outage. Starlette's
# StaticFiles already handles HEAD, which is why /assets never had the fault.
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
@app.api_route("/display", methods=["GET", "HEAD"], include_in_schema=False)
async def index() -> HTMLResponse:
    """The single-page shell, for both the control view and the wall display.

    Never cached: it is the only document that names the current hashed
    bundle, so a stale copy pins the browser to a previous deploy.
    """
    return HTMLResponse(
        (static_dir / "index.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


@app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(static_dir / "favicon.svg", media_type="image/svg+xml")


@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)
async def manifest() -> FileResponse:
    # Without this the browser will not offer "Add to Home Screen", which is
    # what makes the control view launch without Safari's chrome.
    return FileResponse(
        static_dir / "manifest.webmanifest", media_type="application/manifest+json"
    )


# The filenames Vite wrote into the shell. Read per request rather than at
# import, so `scripts/build-ui.sh` is picked up without a restart — the same
# reason index() re-reads the shell each time.
ASSET_REFERENCE = re.compile(r"/assets/([^\"\']+\.(?:js|css))")


@app.api_route("/control/version", methods=["GET", "HEAD"])
async def control_version() -> JSONResponse:
    """Which build this gateway is currently handing out.

    Hashed asset URLs are served `immutable` for a year, which is what makes
    the interface fast over a tunnel and also means a client holding a stale
    index.html keeps fetching its old bundle successfully — from a CDN edge,
    even after the origin has deleted the file. The result is a working but
    permanently out-of-date app with no visible symptom. A kiosk browser never
    gets a manual reload, so without this it would run its first build forever.

    Unauthenticated on purpose: it returns only the filenames already written
    into the HTML that any visitor is served, so it discloses nothing new, and
    the lock screen must be able to check it too.
    """
    shell = (static_dir / "index.html").read_text(encoding="utf-8")
    return JSONResponse(
        {"assets": sorted(set(ASSET_REFERENCE.findall(shell)))},
        headers={"Cache-Control": "no-store"},
    )


@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health() -> dict[str, Any]:
    engine, image_engine = await asyncio.gather(
        control.engine_status(), images.engine_status()
    )
    # An unloaded inference worker is a valid controller state.
    return {"ok": True, "engine": engine, "image_engine": image_engine}


@app.api_route("/control/status", methods=["GET", "HEAD"])
async def control_status() -> dict[str, Any]:
    """Unauthenticated: does this install have an account yet?

    Deliberately returns nothing but the flag. The profile name and email are
    behind authentication, so an unconfigured port does not leak who owns it.
    """
    return {"initialized": credentials.initialized}


@app.post("/control/setup")
async def setup_account(body: dict[str, Any]) -> dict[str, Any]:
    """Create the first account. Refused once one exists."""
    if credentials.initialized:
        raise HTTPException(status_code=409, detail="Already set up")
    try:
        secret = credentials.initialize(
            name=str(body.get("name") or ""),
            email=str(body.get("email") or ""),
            password=str(body.get("password") or ""),
        )
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token, expires_at = credentials.create_session()
    # The first API key is shown once, exactly like any later one.
    return {
        "ok": True,
        "token": token,
        "expires_at": expires_at,
        "secret": secret,
        "profile": credentials.profile(),
    }


@app.post("/control/profile", dependencies=[Depends(require_admin)])
async def update_profile(body: dict[str, Any]) -> dict[str, Any]:
    try:
        profile = credentials.update_profile(
            name=str(body.get("name") or ""), email=str(body.get("email") or "")
        )
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "profile": profile}


@app.post("/control/session")
async def create_session(body: dict[str, Any], request: Request) -> dict[str, Any]:
    """Exchange the admin password for a session token.

    This is the only unauthenticated write endpoint and therefore the single
    credential-guessing surface, so failures are throttled per client address.
    """
    client = request.client.host if request.client else "unknown"
    retry_after = login_throttle.blocked_for(client)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {retry_after}s.",
            headers={"Retry-After": str(retry_after)},
        )
    password = body.get("password")
    if not isinstance(password, str) or not credentials.verify_admin_password(password):
        login_throttle.record_failure(client)
        raise HTTPException(status_code=401, detail="Invalid password")
    login_throttle.reset(client)
    token, expires_at = credentials.create_session()
    return {"token": token, "expires_at": expires_at}


@app.delete("/control/session")
async def end_session(token: str = Depends(require_admin)) -> dict[str, bool]:
    credentials.revoke_session(token)
    return {"ok": True}


@app.get("/control/credentials", dependencies=[Depends(require_admin)])
async def list_credentials() -> dict[str, Any]:
    return {
        "keys": [key.public_dict() for key in credentials.list_keys()],
        "profile": credentials.profile(),
    }


@app.post("/control/credentials/keys", dependencies=[Depends(require_admin)])
async def create_credential_key(body: dict[str, Any]) -> dict[str, Any]:
    key, secret = credentials.create_key(str(body.get("label") or ""))
    # The plaintext is returned exactly once; only its hash is stored.
    return {"ok": True, "key": key.public_dict(), "secret": secret}


@app.delete(
    "/control/credentials/keys/{key_id}", dependencies=[Depends(require_admin)]
)
async def revoke_credential_key(key_id: str) -> dict[str, Any]:
    try:
        credentials.revoke_key(key_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown key") from None
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "keys": [key.public_dict() for key in credentials.list_keys()]}


@app.post("/control/credentials/password", dependencies=[Depends(require_admin)])
async def change_admin_password(body: dict[str, Any]) -> dict[str, Any]:
    current = body.get("current")
    replacement = body.get("password")
    if not isinstance(current, str) or not isinstance(replacement, str):
        raise HTTPException(status_code=400, detail="current and password are required")
    try:
        credentials.change_password(current, replacement)
    except CredentialError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Changing the password clears every session, including the caller's. A
    # fresh token is issued here so the browser is not logged out by its own
    # successful request.
    token, expires_at = credentials.create_session()
    return {"ok": True, "token": token, "expires_at": expires_at}


@app.get("/control/state", dependencies=[Depends(require_admin)])
async def control_state() -> dict[str, Any]:
    # The profile rides along so the avatar monogram stays correct without a
    # second request on every poll.
    return {**await gateway_snapshot(), "profile": credentials.profile()}


@app.get("/control/display", dependencies=[Depends(require_display)])
async def display_state() -> dict[str, Any]:
    """The wall view's whole world, and nothing else.

    Composed as an allow-list rather than by stripping fields off the admin
    snapshot: a field added to a model profile later must not reach a screen
    in a shared room because someone forgot to exclude it. Absent by
    construction are model paths, the assembled argv, search roots, the
    executable location and the account profile.

    Host metrics ride along so a display holds one connection, not two.
    """
    snapshot = await gateway_snapshot()
    active = next((item for item in snapshot["models"] if item.get("active")), None)
    return {
        "engine": {
            "reachable": snapshot["engine"].get("reachable", False),
            "owned": snapshot["engine"].get("owned", False),
            "version": snapshot["engine"].get("version"),
        },
        "runtime": {
            "phase": snapshot["runtime"].get("phase"),
            "error": snapshot["runtime"].get("error"),
        },
        "active": None
        if active is None
        else {
            "id": active["id"],
            "name": active["name"],
            "quantization": active["quantization"],
            "size_gb": active["size_gb"],
            "context_tokens": active["context_tokens"],
            "gpu_layers": active["gpu_layers"],
        },
        "route_count": len(snapshot["models"]),
        "metrics": metrics.snapshot(),
    }


@app.get("/control/events", dependencies=[Depends(require_display)])
async def control_events(since: int | None = None, limit: int = 100) -> dict[str, Any]:
    """What changed, oldest first — the timeline /control/display cannot give.

    Poll with `since` set to the previous response's `cursor`. A client with no
    cursor gets recent history rather than an empty list, so an app opens onto
    something. If `epoch` differs from the one held, the gateway restarted and
    the client should discard its list rather than append to it; `truncated`
    means the cursor fell off the end of a bounded log and events were missed.

    Behind the display token: it is history about the machine, carrying no
    paths, no arguments and no account detail, and reading it cannot change
    anything.
    """
    return events.since(since, limit)


@app.get("/control/display/tokens", dependencies=[Depends(require_admin)])
async def list_display_tokens() -> dict[str, Any]:
    return {"tokens": [token.public_dict() for token in credentials.list_display_tokens()]}


@app.post("/control/display/tokens", dependencies=[Depends(require_admin)])
async def create_display_token(body: dict[str, Any]) -> dict[str, Any]:
    token, secret = credentials.create_display_token(str(body.get("label") or ""))
    # Plaintext exactly once, like an API key; only the hash is stored.
    return {"ok": True, "token": token.public_dict(), "secret": secret}


@app.delete("/control/display/tokens/{token_id}", dependencies=[Depends(require_admin)])
async def revoke_display_token(token_id: str) -> dict[str, Any]:
    try:
        credentials.revoke_display_token(token_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown display token") from None
    return {
        "ok": True,
        "tokens": [token.public_dict() for token in credentials.list_display_tokens()],
    }


@app.get("/control/profiles/{profile_id}", dependencies=[Depends(require_admin)])
async def get_profile(profile_id: str) -> dict[str, Any]:
    try:
        profile = profiles.get(profile_id)
        public = profile.public_dict()
        try:
            public["deployment"] = deployments.get(profile_id).model_dump(mode="json")
        except KeyError:
            public["deployment"] = None
        return {"profile": public}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown profile") from None
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/control/profiles/{profile_id}", dependencies=[Depends(require_admin)])
async def save_profile(profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        deployment_input = body.get("deployment")
        if not isinstance(deployment_input, dict):
            raise ProfileError("Body must contain a deployment")
        model_path = Path(str(body.get("model_path") or ""))
        if not model_path.is_absolute() or model_path.suffix.lower() != ".gguf":
            raise ProfileError("Model path must be an absolute .gguf file")
        prompt_input = body.get("prompt")
        prompt = prompt_input if isinstance(prompt_input, dict) else None
        deployment = DeploymentProfile.model_validate(
            {
                **deployment_input,
                "schema_version": 1,
                "id": profile_id,
                "model": profile_id,
                "engine": "llama.cpp",
            }
        )
        validate_extra_arguments(deployment.advanced.extra_arguments)
        profile = profiles.save(
            profile_id,
            name=str(body.get("name") or ""),
            model_path=str(model_path),
            prompt=prompt,
            notes=str(body.get("notes") or ""),
        )
        deployments.save(deployment)
        control.invalidate_profile(profile_id)
        return {
            "ok": True,
            "profile": profile.public_dict(),
            "state": await gateway_snapshot(),
        }
    except (ProfileError, ValidationError, ValueError, LlamaCppError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# Deliberately sync: FastAPI runs it in a threadpool, so a stalled nvidia-smi
# cannot block the event loop. It is a separate endpoint from /control/state
# for the same reason — a slow sample must not delay routing or model loads.
@app.get("/control/metrics", dependencies=[Depends(require_admin)])
def control_metrics() -> dict[str, Any]:
    return metrics.snapshot()


@app.get("/control/preflight", dependencies=[Depends(require_admin)])
def control_preflight() -> dict[str, Any]:
    # FastAPI runs this synchronous endpoint in its threadpool. Runtime digest,
    # nvidia-smi and filesystem probes must not block the request event loop.
    return preflight.as_dict()


@app.get("/control/discovery/models", dependencies=[Depends(require_admin)])
async def discover_models() -> dict[str, Any]:
    return {"models": profiles.discover_models(), "library": preferences.public_dict()}


@app.get("/control/discovery/image-files", dependencies=[Depends(require_admin)])
async def discover_image_files() -> dict[str, Any]:
    return {
        "files": image_profiles.discover_files(),
        "library": preferences.public_dict(),
    }


@app.get("/control/model-pulls", dependencies=[Depends(require_admin)])
async def model_pull_status() -> dict[str, Any]:
    return model_downloads.status()


async def browse_hub(method: Any, value: str) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(method, value), timeout=25)
    except HfHubHTTPError as exc:
        status = exc.response.status_code if exc.response is not None else 502
        if status in {401, 403, 404}:
            raise HTTPException(status_code=400, detail="Repository unavailable. Check its name and the server's Hugging Face access for private or gated models.") from None
        if status == 429:
            raise HTTPException(status_code=429, detail="Hugging Face is rate-limiting requests. Please try again shortly.") from None
        raise HTTPException(status_code=502, detail="Hugging Face is unavailable. Please try again.") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except Exception:
        raise HTTPException(status_code=502, detail="Could not reach Hugging Face. Please try again.") from None


@app.get("/control/model-browser/search", dependencies=[Depends(require_admin)])
async def search_hub_models(q: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
    return await browse_hub(model_browser.search, q)


@app.get("/control/model-browser/variants", dependencies=[Depends(require_admin)])
async def hub_model_variants(repo: str = Query(min_length=3, max_length=200)) -> dict[str, Any]:
    return await browse_hub(model_browser.variants, repo)


@app.post("/control/model-pulls/preview", dependencies=[Depends(require_admin)])
async def preview_model_pull(body: ModelPullRequest) -> dict[str, Any]:
    try:
        return await asyncio.to_thread(model_downloads.preview, body)
    except DownloadError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post(
    "/control/model-pulls",
    dependencies=[Depends(require_admin)],
    status_code=202,
)
async def start_model_pull(body: ModelPullRequest) -> dict[str, Any]:
    try:
        return {"task": await model_downloads.start(body)}
    except DownloadBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete(
    "/control/model-pulls/{task_id}", dependencies=[Depends(require_admin)]
)
async def cancel_model_pull(task_id: str) -> dict[str, Any]:
    try:
        return {"task": model_downloads.cancel(task_id)}
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown model pull") from None
    except DownloadError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.get("/control/image-profiles/{profile_id}", dependencies=[Depends(require_admin)])
async def get_image_profile(profile_id: str) -> dict[str, Any]:
    try:
        profile = image_profiles.get(profile_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown image profile") from None
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "profile": await images.describe(profile)
    }


@app.put("/control/image-profiles/{profile_id}", dependencies=[Depends(require_admin)])
async def save_image_profile(profile_id: str, body: dict[str, Any]) -> dict[str, Any]:
    try:
        try:
            previous = image_profiles.get(profile_id)
        except KeyError:
            previous = None
        profile = image_profiles.save(
            profile_id,
            name=str(body.get("name") or ""),
            model_path=str(body.get("model_path") or ""),
            text_encoder_path=str(body.get("text_encoder_path") or ""),
            vae_path=str(body.get("vae_path") or ""),
            gpu=int(body.get("gpu", 0)),
            vram_limit_mib=int(body.get("vram_limit_mib", 0)),
            tiled_vae=int(body.get("tiled_vae", 768)),
            offload_to_cpu=body.get("offload_to_cpu", True) is not False,
            default_steps=int(body.get("default_steps", 4)),
            default_cfg_scale=float(body.get("default_cfg_scale", 1.0)),
            default_sampler=str(body.get("default_sampler") or "euler"),
            default_scheduler=str(body.get("default_scheduler") or ""),
            notes=str(body.get("notes") or ""),
        )
    except (ProfileError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    loader_fields = (
        "model_path",
        "text_encoder_path",
        "vae_path",
        "gpu",
        "vram_limit_mib",
        "tiled_vae",
        "offload_to_cpu",
    )
    if previous is None or any(
        getattr(previous, field) != getattr(profile, field) for field in loader_fields
    ):
        images.invalidate_profile(profile_id)
    return {
        "ok": True,
        "profile": await images.describe(profile),
        "state": await gateway_snapshot(),
    }


@app.post("/control/image-models/{model_id}/load", dependencies=[Depends(require_admin)])
async def load_image_model(model_id: str) -> dict[str, Any]:
    try:
        model = image_profiles.resolve(model_id)
        events.record("image.loading", f"Loading {model.name or model.id}", model.id)
        changed = await images.load(model)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown image profile") from None
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (httpx.HTTPError, ImageEngineError) as exc:
        events.record("image.failed", f"Image model failed to load: {exc}", model_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    events.record(
        "image.loaded" if changed else "image.unchanged",
        f"{model.name or model.id} is live" if changed else f"{model.name or model.id} was already live",
        model.id,
    )
    return {"ok": True, "changed": changed, "state": await gateway_snapshot()}


@app.post("/control/image-models/unload", dependencies=[Depends(require_admin)])
async def unload_image_model() -> dict[str, Any]:
    previous = images.state.active_model
    try:
        await images.unload()
    except (httpx.HTTPError, ImageEngineError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    events.record("image.unloaded", "Image model unloaded", previous)
    return {"ok": True, "state": await gateway_snapshot()}


@app.post("/control/preferences/search-roots", dependencies=[Depends(require_admin)])
async def set_search_roots(body: dict[str, Any]) -> dict[str, Any]:
    try:
        library = preferences.set_search_roots(str(body.get("roots") or ""))
    except PreferenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # Discovery reads through the store, so the change takes effect at once.
    profiles.search_roots = preferences.search_roots
    image_profiles.search_roots = preferences.search_roots
    return {"ok": True, "library": library, "models": profiles.discover_models()}


@app.get("/control/logs", dependencies=[Depends(require_admin)])
async def text_engine_logs() -> dict[str, str]:
    try:
        with settings.text_engine_log_path.open("rb") as handle:
            handle.seek(0, 2)
            handle.seek(max(0, handle.tell() - 20_000))
            content = handle.read().decode("utf-8", errors="replace")
    except OSError:
        content = ""
    return {"log": content}


def record_load_outcome(model: RouteProfile, changed: bool) -> None:
    """The feed entry a completed load leaves behind.

    Shared by both modes on purpose: a client reading /control/events must not
    be able to tell whether the operator waited for the load or not.
    """
    label = model.name or model.id
    events.record(
        "model.loaded" if changed else "model.unchanged",
        f"{label} is live" if changed else f"{label} was already live",
        model.id,
    )


def record_load_failure(model: RouteProfile, exc: BaseException) -> None:
    label = model.name or model.id
    events.record("model.failed", f"{label} failed to load: {exc}", model.id)


def record_unload_started(previous: str | None) -> None:
    events.record(
        "model.unloading",
        f"Unloading {previous}" if previous else "Releasing VRAM",
        previous,
    )


def record_unload_outcome(previous: str | None) -> None:
    events.record(
        "model.unloaded",
        f"Unloaded {previous}" if previous else "VRAM released",
        previous,
    )


def record_unload_failure(previous: str | None, exc: BaseException) -> None:
    events.record("model.failed", f"Unload failed: {exc}", previous)


async def run_load_detached(model: RouteProfile) -> None:
    """Carry a load to completion with the event log as its only reporter.

    Nothing awaits this, so a failure has nowhere else to surface — hence the
    bare `Exception`, where the waited path lets an unexpected error become a
    500. CancelledError is deliberately not caught: shutdown is not a failed
    load and must not be written to the feed as one.
    """
    try:
        changed = await control.load(model)
    except Exception as exc:
        record_load_failure(model, exc)
    else:
        record_load_outcome(model, changed)


async def run_unload_detached(previous: str | None) -> None:
    """The unload half of run_load_detached, under the same rules."""
    try:
        await control.unload()
    except Exception as exc:
        record_unload_failure(previous, exc)
    else:
        record_unload_outcome(previous)


def describe_background(operation: dict[str, str | None]) -> str:
    model = operation.get("model")
    if operation["kind"] == "load":
        return f"a load of {model!r}"
    return f"an unload of {model!r}" if model else "an unload"


@app.post("/control/models/{model_id}/load", dependencies=[Depends(require_admin)])
async def load_model(model_id: str, response: Response, wait: bool = True) -> dict[str, Any]:
    """Load a profile, waiting for it or not.

    `wait=true` is the default and is unchanged: the response arrives when the
    worker is ready, which is what the web UI's Load button wants, since it is
    a button a person is watching.

    `wait=false` returns 202 as soon as the load is accepted and reports the
    outcome through /control/events instead. That exists for callers that
    cannot hold a request open for the length of a model load — an iOS App
    Intent runs on a budget, and a Shortcut invoked from a widget or from Siri
    is not a place to block for a minute. It also keeps a cold start well
    inside a reverse proxy's idle timeout, which a load approaching
    MODEL_LOAD_TIMEOUT_SECONDS otherwise would not.

    A model that is already live is answered 200 either way, having done
    nothing: "accepted" says whether a background load actually started, so a
    caller knows whether to expect a terminal event or not.
    """
    try:
        model = profiles.resolve(model_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Unknown model profile") from None
    except ProfileError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    label = model.name or model.id
    if wait:
        events.record("model.loading", f"Loading {label}", model.id)
        try:
            changed = await control.load(model)
        except (httpx.HTTPError, LlamaCppError) as exc:
            # Recorded before the response, so a failure a client never saw the
            # reply to still appears in the history it polls next.
            record_load_failure(model, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        record_load_outcome(model, changed)
        return {
            "ok": True,
            "accepted": False,
            "changed": changed,
            "state": await gateway_snapshot(),
        }

    # Asked before starting anything: an already-live profile must not be
    # torn down and rebuilt just because the caller did not want to wait.
    if control.is_live(model.id):
        return {
            "ok": True,
            "accepted": False,
            "changed": False,
            "state": await gateway_snapshot(),
        }

    in_flight = control.background_operation()
    if in_flight is not None:
        if in_flight["kind"] == "load" and in_flight["model"] == model.id:
            response.status_code = 202
            return {
                "ok": True,
                "accepted": False,
                "changed": False,
                "state": await gateway_snapshot(),
            }
        # Queueing this behind the operation in flight would thrash the GPU on
        # the way to a result nobody asked for. The caller decides instead,
        # once it sees that operation's terminal event.
        raise HTTPException(
            status_code=409,
            detail=f"{describe_background(in_flight).capitalize()} is already in flight",
        )

    events.record("model.loading", f"Loading {label}", model.id)
    control.start_background("load", model.id, run_load_detached(model))
    response.status_code = 202
    return {
        "ok": True,
        "accepted": True,
        "changed": False,
        "state": await gateway_snapshot(),
    }


@app.post("/control/models/unload", dependencies=[Depends(require_admin)])
async def unload_model(response: Response, wait: bool = True) -> dict[str, Any]:
    """Release the worker, waiting for it or not.

    The same two modes as load, for the same reason: stopping a worker means
    SIGTERM, then up to ENGRAI_ENGINE_STOP_TIMEOUT_SECONDS of grace, then SIGKILL, so
    a caller on an execution budget cannot be asked to hold the request open
    for it either.

    Unlike load there is no "already satisfied" short circuit. An unload with
    nothing running is already idempotent — it resets runtime state a caller
    may well be asking for — and skipping it in one mode only would make the
    two modes disagree about what an unload does.
    """
    previous = control.state.active_model

    if wait:
        record_unload_started(previous)
        try:
            await control.unload()
        except (httpx.HTTPError, LlamaCppError) as exc:
            record_unload_failure(previous, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        record_unload_outcome(previous)
        return {"ok": True, "accepted": False, "state": await gateway_snapshot()}

    in_flight = control.background_operation()
    if in_flight is not None:
        if in_flight["kind"] == "unload":
            response.status_code = 202
            return {"ok": True, "accepted": False, "state": await gateway_snapshot()}
        # A detached unload does not cancel a load in flight. Interrupting one
        # means killing a worker mid-start, which is a different change than
        # this; refusing is honest, and the caller can retry once the load
        # lands its terminal event.
        raise HTTPException(
            status_code=409,
            detail=f"{describe_background(in_flight).capitalize()} is already in flight",
        )

    record_unload_started(previous)
    control.start_background("unload", previous, run_unload_detached(previous))
    response.status_code = 202
    return {"ok": True, "accepted": True, "state": await gateway_snapshot()}


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models() -> dict[str, Any]:
    now = int(time.time())
    return {
        "object": "list",
        "data": [
            {
                "id": model.id,
                "object": "model",
                "created": now,
                "owned_by": "self",
            }
            for model in profiles.list() if model.routable
        ]
        + [
            {
                "id": model.id,
                "object": "model",
                "created": now,
                "owned_by": "self-image",
            }
            for model in image_profiles.list()
            if model.routable
        ],
    }


def _purge_expired_image_results(now: float) -> None:
    directory = settings.image_result_dir
    if not directory.exists():
        return
    for path in directory.glob("*.png"):
        try:
            if now - path.stat().st_mtime > settings.image_result_ttl_seconds:
                path.unlink()
        except OSError:
            continue


def _public_image_result_url(request: Request, token: str) -> str:
    """Build the URL the external client sees, not the WireGuard hop's URL.

    Caddy terminates HTTPS on the VPS and reaches Uvicorn over HTTP. The
    forwarded-peer allow-list may lag behind a network move, so url_for can
    otherwise emit http:// and trigger iOS App Transport Security. These
    headers affect only the URL returned to the same caller; they grant no
    access and the random result token remains the authority.
    """
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    scheme = forwarded_proto if forwarded_proto in {"http", "https"} else request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    host = forwarded_host or request.headers.get("host", "") or request.url.netloc
    return f"{scheme}://{host}/v1/images/results/{token}.png"


@app.get("/v1/images/results/{token}.png", name="image_result")
async def image_result(token: str) -> FileResponse:
    """A short-lived, unguessable URL for clients requesting URL output.

    Image tags and native image loaders cannot attach the API bearer token.
    Possession of this 256-bit token therefore authorises this one result,
    matching the short-lived URL behaviour of hosted OpenAI image APIs.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise HTTPException(status_code=404, detail="Image not found")
    path = settings.image_result_dir / f"{token}.png"
    try:
        expired = time.time() - path.stat().st_mtime > settings.image_result_ttl_seconds
    except OSError:
        raise HTTPException(status_code=404, detail="Image not found") from None
    if expired:
        try:
            path.unlink()
        except OSError:
            pass
        raise HTTPException(status_code=404, detail="Image expired")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=3600"})


@app.post(
    "/images/generations",
    dependencies=[Depends(require_api_key)],
    include_in_schema=False,
)
@app.post("/v1/images/generations", dependencies=[Depends(require_api_key)])
async def image_generations(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Request body is not valid JSON"}},
        )
    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Request body must be an object"}},
        )
    response_format = payload.get("response_format", "url")
    if response_format not in {"b64_json", "url"}:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "response_format must be b64_json or url"}},
        )
    try:
        generated = await images.generate(payload)
        data: list[dict[str, Any]] = []
        now = time.time()
        _purge_expired_image_results(now)
        settings.image_result_dir.mkdir(parents=True, exist_ok=True)
        for item in generated:
            encoded = item["b64_json"]
            if encoded.startswith("data:"):
                encoded = encoded.split(",", 1)[-1]
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ImageEngineError("image worker returned invalid image data") from exc
            token = secrets.token_urlsafe(32)
            (settings.image_result_dir / f"{token}.png").write_bytes(content)
            # Return both legal OpenAI image representations. Some Swift
            # clients model `url` as required, while other clients ask for
            # b64_json explicitly. Supplying both makes either decoder work
            # and avoids regenerating an image just to change transport.
            data.append(
                {
                    "url": _public_image_result_url(request, token),
                    "b64_json": encoded,
                    "revised_prompt": str(payload.get("prompt") or ""),
                    "seed": item["seed"],
                    "model": item["model"],
                }
            )
        return JSONResponse({"created": int(time.time()), "data": data})
    except KeyError as exc:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"Image model {str(exc.args[0])!r} is not registered",
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": "model_not_found",
                }
            },
        )
    except ProfileError as exc:
        return JSONResponse(status_code=400, content={"error": {"message": str(exc)}})
    except ImageEngineError as exc:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": str(exc), "type": "invalid_request_error"}},
        )
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )


@app.api_route(
    "/v1/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    dependencies=[Depends(require_api_key)],
)
async def openai_proxy(path: str, request: Request):
    try:
        response, stream = await control.forward_openai(
            request.method,
            path,
            await request.body(),
            request.url.query,
            request.headers.get("content-type"),
        )
        excluded = {"content-length", "content-encoding", "transfer-encoding", "connection"}
        headers = {
            key: value for key, value in response.headers.items() if key.lower() not in excluded
        }
        return StreamingResponse(
            stream,
            status_code=response.status_code,
            headers=headers,
            media_type=response.headers.get("content-type"),
        )
    except KeyError as exc:
        model_id = str(exc.args[0])
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "message": f"Model {model_id!r} is not registered or is disabled",
                    "type": "invalid_request_error",
                    "param": "model",
                    "code": "model_not_found",
                }
            },
        )
    except json.JSONDecodeError:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Request body is not valid JSON"}},
        )
    except (httpx.HTTPError, LlamaCppError) as exc:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "upstream_error"}},
        )
