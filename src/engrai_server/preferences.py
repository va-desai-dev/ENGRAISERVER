"""User-editable server settings, owned by the app rather than the env.

Only the model search roots so far. Kept separate from the credential store
because nothing here is secret, and separate from Settings because Settings is
environment-derived and immutable once loaded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class PreferenceError(ValueError):
    pass


class PreferenceStore:
    def __init__(
        self,
        path: Path,
        *,
        default_roots: list[Path],
        required_roots: list[Path] | None = None,
    ):
        self.path = path
        self.required_roots = required_roots or []
        self.default_roots = self._merge(default_roots, self.required_roots)
        self._data = self._load()

    @staticmethod
    def _merge(first: list[Path], second: list[Path]) -> list[Path]:
        merged: list[Path] = []
        for path in [*first, *second]:
            candidate = path.expanduser()
            resolved = candidate.resolve()
            if any(
                resolved == current.resolve()
                or resolved.is_relative_to(current.resolve())
                for current in merged
            ):
                continue
            merged = [
                current
                for current in merged
                if not current.resolve().is_relative_to(resolved)
            ]
            merged.append(candidate)
        return merged

    def _load(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self._data, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)

    @property
    def search_roots(self) -> list[Path]:
        """Configured roots, or the environment's when none are set."""
        stored = self._data.get("model_search_roots")
        if not isinstance(stored, list) or not stored:
            return self.default_roots
        configured = [Path(str(item)).expanduser() for item in stored if str(item).strip()]
        return self._merge(configured, self.required_roots)

    def public_dict(self) -> dict[str, Any]:
        configured = self._data.get("model_search_roots") or []
        return {
            "model_search_roots": [str(root) for root in self.search_roots],
            "is_default": not configured,
            "default_roots": [str(root) for root in self.default_roots],
        }

    def set_search_roots(self, raw: str) -> dict[str, Any]:
        """Replace the search roots. An empty value restores the default."""
        roots = [item.strip() for item in raw.replace("\n", ":").split(":") if item.strip()]
        resolved: list[str] = []
        for item in roots:
            path = Path(item).expanduser()
            if not path.is_absolute():
                raise PreferenceError(f"{item} is not an absolute path")
            if not path.is_dir():
                raise PreferenceError(f"{path} is not a directory")
            resolved.append(str(path))
        self._data["model_search_roots"] = resolved
        self._write()
        return self.public_dict()
