"""The ledger's vector index: one file, brute-force cosine search.

A personal ledger is hundreds to a few thousand entries, so a linear scan with
``math.sumprod`` over unit vectors answers in milliseconds and needs neither
numpy nor a vector database. Each row remembers the hash of the text it was
embedded from, so only edited entries are embedded again.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import uuid
from array import array
from collections.abc import Iterable, Sequence
from pathlib import Path


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def unit(vector: Sequence[float]) -> array:
    norm = math.sqrt(math.sumprod(vector, vector))
    if not norm:
        raise ValueError("the embedding model returned a zero vector")
    return array("f", (value / norm for value in vector))


class VectorIndex:
    def __init__(self, path: Path):
        self.path = path
        self.fingerprint = ""
        self.dimension = 0
        self.rows: dict[str, tuple[str, array]] = {}

    def load(self) -> None:
        """Read the index, or start empty: it can always be rebuilt."""

        self.fingerprint, self.dimension, self.rows = "", 0, {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            rows: dict[str, tuple[str, array]] = {}
            for entry_id, row in raw["rows"].items():
                vector = array("f")
                vector.frombytes(base64.b64decode(row["vector"]))
                if len(vector) != raw["dimension"]:
                    raise ValueError("vector length does not match the index")
                rows[str(entry_id)] = (str(row["sha"]), vector)
            self.fingerprint = str(raw["fingerprint"])
            self.dimension = int(raw["dimension"])
            self.rows = rows
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            self.fingerprint, self.dimension, self.rows = "", 0, {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        document = {
            "fingerprint": self.fingerprint,
            "dimension": self.dimension,
            "rows": {
                entry_id: {"sha": sha, "vector": base64.b64encode(vector.tobytes()).decode("ascii")}
                for entry_id, (sha, vector) in self.rows.items()
            },
        }
        temporary = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def reset(self, fingerprint: str) -> None:
        self.fingerprint, self.dimension, self.rows = fingerprint, 0, {}

    def current(self, entry_id: str, text: str) -> bool:
        row = self.rows.get(entry_id)
        return row is not None and row[0] == text_hash(text)

    def put(self, entry_id: str, text: str, vector: Sequence[float]) -> None:
        normalized = unit(vector)
        if self.dimension and len(normalized) != self.dimension:
            raise ValueError("the embedding model changed dimension; rebuild the index")
        self.dimension = len(normalized)
        self.rows[entry_id] = (text_hash(text), normalized)

    def prune(self, keep: Iterable[str]) -> bool:
        wanted = set(keep)
        stale = [entry_id for entry_id in self.rows if entry_id not in wanted]
        for entry_id in stale:
            del self.rows[entry_id]
        return bool(stale)

    def search(
        self,
        query: Sequence[float],
        candidates: Iterable[str],
        *,
        top_k: int,
        min_score: float,
    ) -> list[tuple[str, float]]:
        """The best ``top_k`` candidates by cosine similarity, best first."""

        if not self.dimension or len(query) != self.dimension:
            return []
        needle = unit(query)
        scored = [
            (entry_id, math.sumprod(needle, self.rows[entry_id][1]))
            for entry_id in candidates
            if entry_id in self.rows
        ]
        scored = [item for item in scored if item[1] >= min_score]
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]
