"""Portable model catalog records.

These types describe model identity and behavior. They deliberately cannot
represent an absolute host path, GPU allocation, or engine command line.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


ModelTask = Literal["chat", "completion", "embedding", "diffusion", "vision", "audio"]


class Artifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")

    @field_validator("filename")
    @classmethod
    def portable_filename(cls, value: str) -> str:
        if value.startswith(("/", "~")) or "\\" in value or ".." in value.split("/"):
            raise ValueError("artifact filenames must be portable relative paths")
        return value


class ModelSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["huggingface"] = "huggingface"
    repository: str = Field(min_length=3, pattern=r"^[^/\s]+/[^/\s]+$")
    revision: str = Field(min_length=7)


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat: bool = False
    completion: bool = False
    reasoning: bool = False
    vision: bool = False
    image_generation: bool = False


class PromptPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template: Literal["metadata", "jinja", "none"] = "metadata"
    adapter: str = "auto"
    thinking: bool = False


class ModelLimits(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommended_context: int | None = Field(default=None, ge=1)
    maximum_context: int | None = Field(default=None, ge=1)


class ModelManifest(BaseModel):
    """Versioned, portable identity for one model deployment candidate."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    name: str = Field(min_length=1)
    task: ModelTask
    format: Literal["gguf", "safetensors", "diffusers"]
    architecture: str | None = None
    quantization: str | None = None
    license: str | None = None
    source: ModelSource
    artifacts: dict[str, Artifact]
    capabilities: ModelCapabilities = Field(default_factory=ModelCapabilities)
    prompt: PromptPolicy | None = None
    limits: ModelLimits = Field(default_factory=ModelLimits)
    required_engine_capabilities: list[str] = Field(default_factory=list)

    @field_validator("artifacts")
    @classmethod
    def require_primary_artifact(cls, value: dict[str, Artifact]) -> dict[str, Artifact]:
        if "model" not in value:
            raise ValueError("artifacts must define the 'model' role")
        return value
