from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .domain.models import PromptPolicy


PROFILE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RouteProfile:
    """One client-facing route: a model file plus its prompt policy.

    Everything about how the route is launched (devices, context, cache,
    generation defaults) lives in its typed deployment, stored separately.
    """

    id: str
    filename: str
    path: Path
    name: str
    model_path: Path
    prompt: PromptPolicy | None
    notes: str
    modified_at: float

    @property
    def routable(self) -> bool:
        return self.model_path.is_file() and self.model_path.suffix.lower() == ".gguf"

    @property
    def missing_paths(self) -> list[Path]:
        return [] if self.model_path.is_file() else [self.model_path]

    def public_dict(self) -> dict[str, Any]:
        size_gb = None
        if self.model_path.is_file():
            try:
                size_gb = round(self.model_path.stat().st_size / 1_000_000_000, 2)
            except OSError:
                pass
        return {
            "id": self.id,
            "filename": self.filename,
            "name": self.name,
            "model_path": str(self.model_path),
            "model_filename": self.model_path.name,
            "prompt": self.prompt.model_dump(mode="json") if self.prompt else None,
            "size_gb": size_gb,
            # Filled in from the route's deployment by the control plane.
            "context_tokens": 0,
            "gpu_layers": 0,
            "quantization": infer_quantization(self.model_path.name),
            "routable": self.routable,
            "missing_paths": [str(path) for path in self.missing_paths],
            "notes": self.notes,
            "modified_at": self.modified_at,
        }


def prettify_model_name(filename: str) -> str:
    name = re.sub(r"\.gguf$", "", filename, flags=re.IGNORECASE)
    name = re.sub(
        r"[-_.](?:UD-)?(?:IQ\d(?:_[A-Z]+)?|Q\d(?:_[A-Z0-9]+)+|F16|BF16)$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    return name.replace("_", " ").replace("-", " ").strip() or filename


def infer_quantization(filename: str) -> str:
    match = re.search(
        r"(?:UD-)?(?:IQ\d(?:_[A-Z]+)?|Q\d(?:_[A-Z0-9]+)+|F16|BF16)(?=\.gguf$)",
        filename,
        flags=re.IGNORECASE,
    )
    return match.group(0).upper() if match else "GGUF"


class ProfileStore:
    def __init__(self, directory: Path, search_roots: list[Path]):
        self.directory = directory.resolve()
        self.search_roots = search_roots
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, profile_id: str) -> Path:
        if not PROFILE_ID.fullmatch(profile_id):
            raise ProfileError(
                "Route ID must use letters, numbers, dots, dashes, or underscores"
            )
        path = (self.directory / f"{profile_id}.json").resolve()
        if path.parent != self.directory:
            raise ProfileError("Profile path escapes the configured directory")
        return path

    def get(self, profile_id: str) -> RouteProfile:
        path = self._path(profile_id)
        if not path.is_file():
            raise KeyError(profile_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"Cannot read {path.name}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ProfileError(f"{path.name} must contain one JSON object")
        if payload.get("schema_version") != 2:
            raise ProfileError(
                f"{path.name} uses unsupported schema_version "
                f"{payload.get('schema_version')!r}; recreate the route in the web UI"
            )
        model_path = payload.get("model_path")
        if not isinstance(model_path, str) or not model_path:
            raise ProfileError(f"{path.name} has no model_path")
        return RouteProfile(
            id=profile_id,
            filename=path.name,
            path=path,
            name=str(payload.get("name") or prettify_model_name(Path(model_path).name)),
            model_path=Path(model_path),
            prompt=PromptPolicy.model_validate(payload["prompt"])
            if isinstance(payload.get("prompt"), dict)
            else None,
            notes=str(payload.get("notes") or ""),
            modified_at=path.stat().st_mtime,
        )

    def list(self) -> list[RouteProfile]:
        profiles: list[RouteProfile] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                profiles.append(self.get(path.stem))
            except ProfileError:
                continue
        return profiles

    def resolve(self, profile_id: str) -> RouteProfile:
        profile = self.get(profile_id)
        if not profile.routable:
            raise ProfileError(
                f"Route {profile_id!r} does not point to a readable GGUF model"
            )
        return profile

    def save(
        self,
        profile_id: str,
        *,
        name: str,
        model_path: str,
        prompt: PromptPolicy | dict[str, Any] | None = None,
        notes: str = "",
    ) -> RouteProfile:
        model = Path(model_path)
        if not model.is_absolute():
            raise ProfileError("Model path must be absolute")
        if model.suffix.lower() != ".gguf":
            raise ProfileError("Model path must end in .gguf")
        prompt_policy = (
            prompt
            if isinstance(prompt, PromptPolicy)
            else PromptPolicy.model_validate(prompt)
            if prompt is not None
            else None
        )
        payload: dict[str, Any] = {
            "schema_version": 2,
            "name": name.strip() or prettify_model_name(model.name),
            "model_path": str(model),
            "notes": notes.strip(),
        }
        if prompt_policy:
            payload["prompt"] = prompt_policy.model_dump(mode="json")
        path = self._path(profile_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return self.get(profile_id)

    def discover_models(self) -> list[dict[str, Any]]:
        # In the Hugging Face cache the real bytes live in blobs/ under
        # content-hash names with no extension, and snapshots/<rev>/ holds
        # symlinks with the real filenames. Globbing for *.gguf therefore sees
        # each file exactly once, through the snapshot, which is also the path
        # a profile should store.
        discovered: dict[str, dict[str, Any]] = {}
        for root in self.search_roots:
            if not root.exists():
                continue
            for path in root.rglob("*.gguf"):
                try:
                    resolved = path.resolve(strict=True)
                    stat = resolved.stat()
                except OSError:
                    continue
                lower = path.name.lower()
                if "mmproj" in lower:
                    kind = "projector"
                elif path.parent.name.lower() == "mtp" or lower.startswith("mtp-"):
                    kind = "draft"
                elif "flux" in lower:
                    kind = "image"
                else:
                    kind = "text"
                discovered[str(path)] = {
                    "path": str(path),
                    "filename": path.name,
                    "name": prettify_model_name(path.name),
                    "quantization": infer_quantization(path.name),
                    "size_gb": round(stat.st_size / 1_000_000_000, 2),
                    "kind": kind,
                }
        return sorted(
            discovered.values(),
            key=lambda item: (item["kind"] != "text", item["name"].lower()),
        )
