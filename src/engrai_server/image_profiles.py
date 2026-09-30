from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .profiles import PROFILE_ID, ProfileError, infer_quantization, prettify_model_name


IMAGE_SUFFIXES = {".gguf", ".safetensors"}


@dataclass(frozen=True, slots=True)
class ImageProfile:
    """The small, stable set of files needed by an image worker."""

    id: str
    filename: str
    path: Path
    name: str
    model_path: Path
    text_encoder_path: Path
    vae_path: Path
    gpu: int
    vram_limit_mib: int
    tiled_vae: int
    offload_to_cpu: bool
    default_steps: int
    default_cfg_scale: float
    default_sampler: str
    default_scheduler: str
    notes: str
    modified_at: float

    @property
    def required_paths(self) -> list[Path]:
        return [self.model_path, self.text_encoder_path, self.vae_path]

    @property
    def missing_paths(self) -> list[Path]:
        return [path for path in self.required_paths if not path.is_file()]

    @property
    def routable(self) -> bool:
        return not self.missing_paths and self.model_path.suffix.lower() in IMAGE_SUFFIXES

    def public_dict(self, command: list[str] | None = None) -> dict[str, Any]:
        """Describe the profile for the UI; ``command`` is the compiled launch, if known."""

        try:
            size_gb = round(self.model_path.stat().st_size / 1_000_000_000, 2)
        except OSError:
            size_gb = None
        return {
            "id": self.id,
            "filename": self.filename,
            "name": self.name,
            "model_path": str(self.model_path),
            "model_filename": self.model_path.name,
            "text_encoder_path": str(self.text_encoder_path),
            "vae_path": str(self.vae_path),
            "gpu": self.gpu,
            "vram_limit_mib": self.vram_limit_mib,
            "tiled_vae": self.tiled_vae,
            "offload_to_cpu": self.offload_to_cpu,
            "default_steps": self.default_steps,
            "default_cfg_scale": self.default_cfg_scale,
            "default_sampler": self.default_sampler,
            "default_scheduler": self.default_scheduler,
            "command": shlex.join(command) if command else None,
            "size_gb": size_gb,
            "quantization": infer_quantization(self.model_path.name),
            "routable": self.routable,
            "missing_paths": [str(path) for path in self.missing_paths],
            "notes": self.notes,
            "modified_at": self.modified_at,
        }


class ImageProfileStore:
    def __init__(self, directory: Path, search_roots: list[Path]):
        self.directory = directory.resolve()
        self.search_roots = search_roots
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, profile_id: str) -> Path:
        if not PROFILE_ID.fullmatch(profile_id):
            raise ProfileError(
                "Image model ID must use letters, numbers, dots, dashes, or underscores"
            )
        path = (self.directory / f"{profile_id}.json").resolve()
        if path.parent != self.directory:
            raise ProfileError("Image profile path escapes the configured directory")
        return path

    def get(self, profile_id: str) -> ImageProfile:
        path = self._path(profile_id)
        if not path.is_file():
            raise KeyError(profile_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProfileError(f"Cannot read {path.name}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version", 1) != 1:
            raise ProfileError(f"{path.name} is not a supported image profile")

        values: dict[str, Path] = {}
        for key in ("model_path", "text_encoder_path", "vae_path"):
            value = payload.get(key)
            if not isinstance(value, str) or not value:
                raise ProfileError(f"{path.name} has no {key}")
            candidate = Path(value)
            if not candidate.is_absolute():
                raise ProfileError(f"{key} must be an absolute path")
            if candidate.suffix.lower() not in IMAGE_SUFFIXES:
                raise ProfileError(f"{key} must be a GGUF or safetensors file")
            values[key] = candidate

        return ImageProfile(
            id=profile_id,
            filename=path.name,
            path=path,
            name=str(payload.get("name") or prettify_model_name(values["model_path"].name)),
            model_path=values["model_path"],
            text_encoder_path=values["text_encoder_path"],
            vae_path=values["vae_path"],
            gpu=max(0, int(payload.get("gpu", 0))),
            vram_limit_mib=max(0, int(payload.get("vram_limit_mib", 0))),
            tiled_vae=max(0, int(payload.get("tiled_vae", 768))),
            offload_to_cpu=bool(payload.get("offload_to_cpu", True)),
            default_steps=int(payload.get("default_steps", 4)),
            default_cfg_scale=float(payload.get("default_cfg_scale", 1.0)),
            default_sampler=str(payload.get("default_sampler") or "euler").strip(),
            default_scheduler=str(payload.get("default_scheduler") or "").strip(),
            notes=str(payload.get("notes") or ""),
            modified_at=path.stat().st_mtime,
        )

    def list(self) -> list[ImageProfile]:
        profiles: list[ImageProfile] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                profiles.append(self.get(path.stem))
            except ProfileError:
                continue
        return profiles

    def resolve(self, profile_id: str) -> ImageProfile:
        profile = self.get(profile_id)
        if not profile.routable:
            missing = ", ".join(str(path) for path in profile.missing_paths)
            raise ProfileError(f"Image model {profile_id!r} is missing: {missing}")
        return profile

    def save(
        self,
        profile_id: str,
        *,
        name: str,
        model_path: str,
        text_encoder_path: str,
        vae_path: str,
        gpu: int = 0,
        vram_limit_mib: int = 0,
        tiled_vae: int = 768,
        offload_to_cpu: bool = True,
        default_steps: int = 4,
        default_cfg_scale: float = 1.0,
        default_sampler: str = "euler",
        default_scheduler: str = "",
        notes: str = "",
    ) -> ImageProfile:
        paths = {
            "model_path": Path(model_path),
            "text_encoder_path": Path(text_encoder_path),
            "vae_path": Path(vae_path),
        }
        for key, path in paths.items():
            if not path.is_absolute():
                raise ProfileError(f"{key} must be an absolute path")
            if path.suffix.lower() not in IMAGE_SUFFIXES:
                raise ProfileError(f"{key} must be a GGUF or safetensors file")
        if not 0 <= gpu <= 31:
            raise ProfileError("GPU must be between 0 and 31")
        if not 0 <= vram_limit_mib <= 1_000_000:
            raise ProfileError("VRAM limit is not valid")
        if not 0 <= tiled_vae <= 16_384:
            raise ProfileError("Tiled VAE threshold is not valid")
        if not 1 <= default_steps <= 150:
            raise ProfileError("Default steps must be from 1 to 150")
        if not 0 <= default_cfg_scale <= 50:
            raise ProfileError("Default CFG must be from 0 to 50")
        default_sampler = default_sampler.strip()
        default_scheduler = default_scheduler.strip()
        for label, value in (
            ("Default sampler", default_sampler),
            ("Default scheduler", default_scheduler),
        ):
            if len(value) > 128 or "\n" in value or "\r" in value:
                raise ProfileError(f"{label} is not valid")

        payload = {
            "schema_version": 1,
            "name": name.strip() or prettify_model_name(paths["model_path"].name),
            **{key: str(path) for key, path in paths.items()},
            "gpu": gpu,
            "vram_limit_mib": vram_limit_mib,
            "tiled_vae": tiled_vae,
            "offload_to_cpu": offload_to_cpu,
            "default_steps": default_steps,
            "default_cfg_scale": default_cfg_scale,
            "default_sampler": default_sampler,
            "default_scheduler": default_scheduler,
            "notes": notes.strip(),
        }
        path = self._path(profile_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)
        return self.get(profile_id)

    def discover_files(self) -> list[dict[str, Any]]:
        discovered: dict[str, dict[str, Any]] = {}
        for root in self.search_roots:
            if not root.exists():
                continue
            for suffix in IMAGE_SUFFIXES:
                for path in root.rglob(f"*{suffix}"):
                    try:
                        resolved = path.resolve(strict=True)
                        stat = resolved.stat()
                    except OSError:
                        continue
                    lower = path.name.lower()
                    if "vae" in lower:
                        kind = "vae"
                    elif "flux" in lower or "stable-diffusion" in lower or "sdxl" in lower:
                        kind = "checkpoint"
                    elif path.suffix.lower() == ".gguf":
                        kind = "encoder"
                    else:
                        kind = "other"
                    discovered[str(path)] = {
                        "path": str(path),
                        "filename": path.name,
                        "name": prettify_model_name(path.name),
                        "quantization": infer_quantization(path.name),
                        "size_gb": round(stat.st_size / 1_000_000_000, 2),
                        "kind": kind,
                    }
        order = {"checkpoint": 0, "encoder": 1, "vae": 2, "other": 3}
        return sorted(
            discovered.values(),
            key=lambda item: (order[str(item["kind"])], str(item["name"]).lower()),
        )
