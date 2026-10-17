"""The ledger as the gateway uses it: storage, indexing, and prompt injection.

Entries and settings are held in memory and written through on every change,
so a chat request never touches disk. Injection is strictly best-effort: a
missing embedding model, a dead worker or a corrupt index reduces what is
added to the prompt, and never fails or delays the request beyond one attempt.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from ..cardgen.store import LibraryStore
from ..events import EventLog
from ..settings import Settings
from . import recall
from .embedder import EmbeddingError, EmbeddingWorker
from .index import VectorIndex
from .models import MAX_ENTRIES, LedgerEntry, LedgerSettings


ENTRIES = "entries"
SETTINGS = "settings"
QUERY_TIMEOUT_SECONDS = 20.0
# After a failed recall, chat requests stop trying for this long, so a broken
# embedding model costs one slow request a minute and not every one.
RECALL_BACKOFF_SECONDS = 60.0


class LedgerError(ValueError):
    """A ledger change was rejected."""


class Embedder(Protocol):
    async def embed(
        self, texts: Sequence[str], *, model_path: str, gpu_layers: int, timeout: float = ...
    ) -> list[list[float]]: ...

    async def status(self) -> dict[str, Any]: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


class Ledger:
    def __init__(
        self,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        events: EventLog | None = None,
        today: Callable[[], date] = date.today,
    ) -> None:
        self.store = LibraryStore(settings.ledger_data_dir)
        self.index = VectorIndex(settings.ledger_data_dir / "index.json")
        self.embedder: Embedder = embedder or EmbeddingWorker(settings)
        self.events = events
        self.today = today
        self.settings = LedgerSettings()
        self.entries: dict[str, LedgerEntry] = {}
        self.index_error: str | None = None
        self._loaded = False
        self._load_lock = asyncio.Lock()
        self._index_lock = asyncio.Lock()
        self._index_task: asyncio.Task[None] | None = None
        self._index_dirty = False
        self._recall_retry_at = 0.0

    # ── Storage ──────────────────────────────────────────────────────────
    async def load(self) -> None:
        if self._loaded:
            return
        async with self._load_lock:
            if self._loaded:
                return
            rows = await self.store.read(ENTRIES)
            entries: dict[str, LedgerEntry] = {}
            for row in rows if isinstance(rows, list) else []:
                # A row this version cannot read stays on disk untouched; it is
                # only left out of what is sent.
                with contextlib.suppress(ValidationError):
                    entry = LedgerEntry.model_validate(row)
                    if entry.id:
                        entries[entry.id] = entry
            stored = await self.store.read(SETTINGS, {})
            try:
                self.settings = LedgerSettings.model_validate(stored)
            except ValidationError:
                self.settings = LedgerSettings()
            self.entries = entries
            await asyncio.to_thread(self.index.load)
            self._loaded = True

    async def save_entry(self, data: dict[str, Any]) -> LedgerEntry:
        await self.load()
        try:
            entry = LedgerEntry.model_validate(data)
        except ValidationError as exc:
            raise LedgerError(_first_error(exc)) from exc
        if entry.id not in self.entries and len(self.entries) >= MAX_ENTRIES:
            raise LedgerError(f"the ledger holds at most {MAX_ENTRIES} entries")
        record = entry.model_dump(exclude={"createdAt", "updatedAt"})
        if record["id"] is None:
            del record["id"]
        saved = LedgerEntry.model_validate(await self.store.save(ENTRIES, record))
        self.entries[str(saved.id)] = saved
        self._schedule_index()
        return saved

    async def remove_entry(self, entry_id: str) -> None:
        await self.load()
        if entry_id not in self.entries:
            raise KeyError(entry_id)
        await self.store.remove(ENTRIES, entry_id)
        del self.entries[entry_id]
        self._schedule_index()

    async def save_settings(self, data: dict[str, Any]) -> LedgerSettings:
        await self.load()
        try:
            updated = LedgerSettings.model_validate(data)
        except ValidationError as exc:
            raise LedgerError(_first_error(exc)) from exc
        document = updated.model_dump(mode="json")
        await self.store.update(SETTINGS, lambda _: document, fallback={})
        previous, self.settings = self.settings, updated
        self._recall_retry_at = 0.0
        if not updated.embedding_model_path:
            self.index_error = None
            if previous.embedding_model_path:
                await self.embedder.stop()
        elif (
            updated.embedding_model_path != previous.embedding_model_path
            or updated.embedding_gpu_layers != previous.embedding_gpu_layers
            or updated.document_prefix != previous.document_prefix
        ):
            self._schedule_index()
        return updated

    # ── Index ────────────────────────────────────────────────────────────
    def _fingerprint(self) -> str:
        """Identifies what produced the stored vectors; a change rebuilds them."""

        path = Path(self.settings.embedding_model_path)
        try:
            stat = path.stat()
        except OSError as exc:
            raise EmbeddingError("the embedding model file is missing") from exc
        return f"{path}|{stat.st_size}|{stat.st_mtime_ns}|{self.settings.document_prefix}"

    def _schedule_index(self) -> None:
        if not self.settings.embedding_model_path:
            return
        self._index_dirty = True
        if self._index_task is None or self._index_task.done():
            self._index_task = asyncio.create_task(self._index_loop())

    async def _index_loop(self) -> None:
        while self._index_dirty:
            self._index_dirty = False
            await self.reindex()

    async def reindex(self, *, force: bool = False) -> None:
        """Embed every entry whose text changed. Failures land in ``index_error``."""

        await self.load()
        async with self._index_lock:
            settings = self.settings
            if not settings.embedding_model_path:
                self.index_error = None
                return
            try:
                fingerprint = await asyncio.to_thread(self._fingerprint)
                changed = force or self.index.fingerprint != fingerprint
                if changed:
                    self.index.reset(fingerprint)
                changed |= self.index.prune(self.entries)
                pending = [
                    entry
                    for entry in self.entries.values()
                    if not self.index.current(str(entry.id), entry.embedded_text)
                ]
                if pending:
                    vectors = await self.embedder.embed(
                        [entry.document(settings.document_prefix) for entry in pending],
                        model_path=settings.embedding_model_path,
                        gpu_layers=settings.embedding_gpu_layers,
                    )
                    for entry, vector in zip(pending, vectors, strict=True):
                        self.index.put(str(entry.id), entry.embedded_text, vector)
                if changed or pending:
                    await asyncio.to_thread(self.index.save)
                self.index_error = None
            except (EmbeddingError, ValueError, OSError) as exc:
                self.index_error = str(exc)

    # ── Recall ───────────────────────────────────────────────────────────
    def _episodes(self) -> list[LedgerEntry]:
        return [entry for entry in self.entries.values() if entry.enabled and not entry.pinned]

    def _pinned(self) -> list[LedgerEntry]:
        return [entry for entry in self.entries.values() if entry.enabled and entry.pinned]

    async def recall(self, text: str) -> list[tuple[LedgerEntry, float]]:
        """Episodes relevant to ``text``, best first, within the token budget."""

        settings = self.settings
        candidates = [
            str(entry.id)
            for entry in self._episodes()
            if self.index.current(str(entry.id), entry.embedded_text)
        ]
        if not settings.embedding_model_path or not candidates or not text.strip():
            return []
        (vector,) = await self.embedder.embed(
            [settings.query_prefix + text],
            model_path=settings.embedding_model_path,
            gpu_layers=settings.embedding_gpu_layers,
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        hits = self.index.search(
            vector, candidates, top_k=settings.top_k, min_score=settings.min_score
        )
        scores = dict(hits)
        kept = recall.within_budget(
            [self.entries[entry_id] for entry_id, _ in hits], settings.recall_budget_tokens
        )
        return [(entry, scores[str(entry.id)]) for entry in kept]

    def _note_failure(self, exc: Exception) -> None:
        self._recall_retry_at = time.monotonic() + RECALL_BACKOFF_SECONDS
        if self.events is not None:
            # Never the query or an entry: only what kind of failure it was.
            self.events.record(
                "ledger.recall_failed",
                f"Ledger recall unavailable ({type(exc).__name__}); sending pinned entries only",
            )

    async def augment(self, route_id: str, payload: dict[str, Any], *, rendered: bool) -> None:
        """Add the ledger to a chat request in place. Never raises.

        ``rendered`` says the gateway will render the prompt itself. The
        native llama-server handler enforces strict template roles, so there
        the recalled block is folded into the user turn.
        """

        try:
            await self.load()
            settings = self.settings
            messages = payload.get("messages")
            if route_id not in settings.enabled_routes or not isinstance(messages, list):
                return
            if not messages:
                return
            recalled: list[LedgerEntry] = []
            if time.monotonic() >= self._recall_retry_at:
                try:
                    hits = await self.recall(recall.query_text(messages))
                    recalled = [entry for entry, _ in hits]
                except Exception as exc:  # noqa: BLE001 - recall must fail open
                    self._note_failure(exc)
            updated = recall.add_recalled(
                messages,
                recall.recalled_block(recalled, settings.subject_name, self.today().isoformat()),
                settings.recall_placement if rendered else "user",
            )
            payload["messages"] = recall.add_pinned(
                updated, recall.pinned_block(self._pinned(), settings.subject_name)
            )
        except Exception as exc:  # noqa: BLE001 - the chat request outranks the ledger
            self._note_failure(exc)

    # ── Views ────────────────────────────────────────────────────────────
    async def preview(self, text: str) -> dict[str, Any]:
        """What a message would pull in, with scores, for tuning recall."""

        await self.load()
        settings = self.settings
        error = None
        hits: list[tuple[LedgerEntry, float]] = []
        try:
            hits = await self.recall(text)
        except Exception as exc:  # noqa: BLE001 - reported to the caller instead
            error = str(exc)
        return {
            "pinned_block": recall.pinned_block(self._pinned(), settings.subject_name),
            "recalled_block": recall.recalled_block(
                [entry for entry, _ in hits], settings.subject_name, self.today().isoformat()
            ),
            "recalled": [
                {"id": entry.id, "title": entry.title, "when": entry.when, "score": round(score, 4)}
                for entry, score in hits
            ],
            "error": error,
        }

    async def snapshot(self) -> dict[str, Any]:
        await self.load()
        settings = self.settings
        ordered = recall.chronological(self.entries.values())
        indexed = sum(
            1 for entry in ordered if self.index.current(str(entry.id), entry.embedded_text)
        )
        return {
            "settings": settings.model_dump(mode="json"),
            "entries": [entry.model_dump(mode="json") for entry in ordered],
            "status": {
                "entries": len(ordered),
                "pinned": len(self._pinned()),
                "episodes": len(self._episodes()),
                "pinned_tokens": recall.estimate_tokens(
                    recall.pinned_block(self._pinned(), settings.subject_name)
                ),
                "indexed": indexed,
                "unindexed": len(ordered) - indexed,
                "dimension": self.index.dimension,
                "indexing": self._index_task is not None and not self._index_task.done(),
                "error": self.index_error,
                "worker": await self.embedder.status(),
            },
        }

    async def close(self) -> None:
        task = self._index_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self.embedder.close()


def _first_error(exc: ValidationError) -> str:
    error = exc.errors()[0]
    field = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "invalid")).removeprefix("Value error, ")
    return f"{field}: {message}" if field else message
