"""File-backed Card Studio library.

One JSON document per kind (``cards.json``, ``prompts.json``, ...), rewritten
whole by atomic rename. Writes to the same document are serialised; reads and
writes run off the event loop because a card record can carry a multi-megabyte
portrait. The layout matches CardGen's per-user directory, so a CardGen data
folder can be copied in unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

KIND = re.compile(r"[\w-]+")

# Served back with the stored MIME type, so anything outside this list would
# turn the library into a same-origin file host (a data:text/html upload would
# render as a page with access to the session token).
ALLOWED_IMAGE_TYPES = frozenset(
    {"image/png", "image/jpeg", "image/webp", "image/gif", "image/avif"}
)
# Roughly a 24 MB image once decoded.
MAX_IMAGE_BASE64_CHARS = 32 * 1024 * 1024
DATA_URL = re.compile(r"data:([^;,]+);base64,(.+)", re.DOTALL)


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StoredImage:
    mime: str
    base64: str

    def decode(self) -> bytes:
        return base64.b64decode(self.base64)


def parse_image(value: object) -> StoredImage | None:
    """A storable data: URL, or None when it is malformed, unsafe or oversized."""
    if not isinstance(value, str):
        return None
    match = DATA_URL.fullmatch(value)
    if not match:
        return None
    mime = match.group(1).strip().lower()
    encoded = match.group(2)
    if mime not in ALLOWED_IMAGE_TYPES or len(encoded) > MAX_IMAGE_BASE64_CHARS:
        return None
    try:
        base64.b64decode(encoded[:4096] + "=" * (-len(encoded[:4096]) % 4), validate=True)
    except (binascii.Error, ValueError):
        return None
    return StoredImage(mime, encoded)


def _strings(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def to_v2(character: object) -> dict[str, Any]:
    """Serialize an editor character as a Character Card V2 document.

    Mirrors ``card-v2.js``: the browser and the store must agree on what a
    saved card is, so both accept the editor's camelCase names and V2's own.
    """
    source = character if isinstance(character, dict) else {}
    if isinstance(source.get("data"), dict):
        source = source["data"]

    def pick(camel: str, snake: str) -> str:
        return _text(source[camel] if source.get(camel) is not None else source.get(snake))

    data: dict[str, Any] = {
        "name": _text(source.get("name")),
        "description": _text(source.get("description")),
        "personality": _text(source.get("personality")),
        "scenario": _text(source.get("scenario")),
        "first_mes": pick("firstMessage", "first_mes"),
        "mes_example": pick("mesExample", "mes_example"),
        "creator_notes": pick("creatorNotes", "creator_notes"),
        "system_prompt": pick("systemPrompt", "system_prompt"),
        "post_history_instructions": pick("postHistoryInstructions", "post_history_instructions"),
        "alternate_greetings": _strings(
            source["alternateGreetings"]
            if source.get("alternateGreetings") is not None
            else source.get("alternate_greetings")
        ),
        "tags": _strings(source.get("tags")),
        "creator": _text(source.get("creator")),
        "character_version": pick("characterVersion", "character_version"),
        "extensions": copy.deepcopy(source["extensions"])
        if isinstance(source.get("extensions"), dict)
        else {},
    }
    if not data["name"].strip():
        raise StoreError("Give the character a name before saving or exporting.")
    if isinstance(source.get("character_book"), dict):
        data["character_book"] = copy.deepcopy(source["character_book"])
    return {"spec": "chara_card_v2", "spec_version": "2.0", "data": data}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class LibraryStore:
    def __init__(self, root: Path):
        self.root = root
        self._locks: dict[str, asyncio.Lock] = {}

    def path(self, kind: str) -> Path:
        if not KIND.fullmatch(kind):
            raise StoreError("Invalid storage key")
        return self.root / f"{kind}.json"

    def _read_sync(self, path: Path, fallback: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return copy.deepcopy(fallback)
        except json.JSONDecodeError as exc:
            # Never overwrite a document that failed to parse: the next write
            # would silently replace the whole library with an empty list.
            raise StoreError(f"{path.name} is unreadable; it has been left untouched") from exc

    def _write_sync(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    async def read(self, kind: str, fallback: Any = None) -> Any:
        return await asyncio.to_thread(
            self._read_sync, self.path(kind), [] if fallback is None else fallback
        )

    async def update(
        self,
        kind: str,
        change: Callable[[Any], Any | Awaitable[Any]],
        fallback: Any = None,
    ) -> Any:
        path = self.path(kind)
        lock = self._locks.setdefault(kind, asyncio.Lock())
        async with lock:
            current = await asyncio.to_thread(
                self._read_sync, path, [] if fallback is None else fallback
            )
            result = change(current)
            if asyncio.iscoroutine(result):
                result = await result
            await asyncio.to_thread(self._write_sync, path, result)
            return result

    async def save(self, kind: str, record: dict[str, Any]) -> dict[str, Any]:
        record_id = str(record.get("id") or f"local_{uuid.uuid4()}")
        saved: dict[str, Any] = {}

        def change(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal saved
            index = next((i for i, row in enumerate(rows) if str(row.get("id")) == record_id), -1)
            previous = rows[index] if index >= 0 else {}
            saved = {
                **previous,
                **record,
                "id": record_id,
                "createdAt": previous.get("createdAt") or record.get("createdAt") or _now(),
                "updatedAt": _now(),
            }
            if index < 0:
                rows.append(saved)
            else:
                rows[index] = saved
            return rows

        await self.update(kind, change)
        return saved

    async def remove(self, kind: str, record_id: str) -> None:
        await self.update(kind, lambda rows: [r for r in rows if str(r.get("id")) != record_id])

    def legacy_image(self, card_id: str) -> StoredImage | None:
        """A portrait CardGen kept beside cards.json rather than inside it."""
        if not KIND.fullmatch(card_id):
            return None
        directory = self.root / "card-images"
        try:
            content = (directory / f"{card_id}.img").read_bytes()
            mime = (directory / f"{card_id}.mime").read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if mime not in ALLOWED_IMAGE_TYPES:
            return None
        return StoredImage(mime, base64.b64encode(content).decode("ascii"))
