"""Control routes for the ledger, behind the console's admin session."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from ..cardgen.store import StoreError
from .service import Ledger, LedgerError


def create_ledger(*, ledger: Ledger, require_admin: Callable[..., Any]) -> Callable[[FastAPI], None]:
    """Build the ledger's routes and return the function that installs them."""

    api = APIRouter(prefix="/control/ledger", dependencies=[Depends(require_admin)])

    async def state() -> dict[str, Any]:
        try:
            return await ledger.snapshot()
        except StoreError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @api.get("")
    async def read_ledger() -> dict[str, Any]:
        return await state()

    @api.put("/settings")
    async def save_settings(body: dict[str, Any]) -> dict[str, Any]:
        try:
            await ledger.save_settings(body)
        except LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **await state()}

    @api.post("/entries")
    async def save_entry(body: dict[str, Any]) -> dict[str, Any]:
        try:
            entry = await ledger.save_entry(body)
        except LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, "entry": entry.model_dump(mode="json"), **await state()}

    @api.delete("/entries/{entry_id}")
    async def remove_entry(entry_id: str) -> dict[str, Any]:
        try:
            await ledger.remove_entry(entry_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Unknown ledger entry") from None
        return {"ok": True, **await state()}

    @api.post("/reindex")
    async def reindex(body: dict[str, Any] | None = None) -> dict[str, Any]:
        await ledger.reindex(force=bool((body or {}).get("force")))
        return {"ok": ledger.index_error is None, **await state()}

    @api.post("/preview")
    async def preview(body: dict[str, Any]) -> dict[str, Any]:
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text is required")
        return await ledger.preview(text)

    def install(app: FastAPI) -> None:
        app.include_router(api)

    return install
