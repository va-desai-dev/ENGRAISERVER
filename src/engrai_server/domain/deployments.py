"""Machine-local deployment choices compiled by engine adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ComputePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["auto", "cuda", "rocm", "metal", "vulkan", "cpu"] = "auto"
    devices: list[int] = Field(default_factory=list)
    gpu_layers: Literal["auto", "all"] | int = "auto"
    split_mode: Literal["none", "layer", "row"] = "layer"
    main_gpu: int | None = Field(default=None, ge=0)
    tensor_split: list[float] = Field(default_factory=list)
    flash_attention: bool = True

    @model_validator(mode="after")
    def validate_allocation(self) -> "ComputePolicy":
        if isinstance(self.gpu_layers, int) and self.gpu_layers < 0:
            raise ValueError("gpu_layers must be non-negative")
        if any(value <= 0 for value in self.tensor_split):
            raise ValueError("tensor_split values must be positive")
        if len(set(self.devices)) != len(self.devices):
            raise ValueError("devices must not contain duplicates")
        if self.tensor_split and self.devices and len(self.tensor_split) != len(self.devices):
            raise ValueError("tensor_split must have one value per selected device")
        if self.main_gpu is not None and self.devices and self.main_gpu not in self.devices:
            raise ValueError("main_gpu must be one of the selected devices")
        if self.split_mode == "none" and len(self.devices) > 1:
            raise ValueError("split_mode 'none' accepts at most one selected device")
        return self


class MemoryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: int | None = Field(default=None, ge=1)
    kv_cache: Literal["f16", "q8_0", "q4_0"] = "q8_0"
    kv_offload: bool = True
    context_shift: bool = True
    cache_reuse_min_tokens: int = Field(default=256, ge=0)
    parallel_slots: int = Field(default=1, ge=1)


class GenerationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_tokens: int | None = Field(default=None, ge=1)
    reasoning_effort: Literal["low", "medium", "high"] | None = None


class AdvancedPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra_arguments: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def reject_process_control(self) -> "AdvancedPolicy":
        forbidden = {"--host", "--port", "--model", "-m", "--ssl", "--admin"}
        if forbidden.intersection(self.extra_arguments):
            raise ValueError("extra_arguments cannot override ENGRAI-owned process controls")
        return self


class DeploymentProfile(BaseModel):
    """Ignored XDG state binding a portable model to one host and engine."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    model: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    engine: Literal["llama.cpp", "stable-diffusion.cpp"]
    engine_version: str | None = None
    compute: ComputePolicy = Field(default_factory=ComputePolicy)
    memory: MemoryPolicy = Field(default_factory=MemoryPolicy)
    generation: GenerationPolicy = Field(default_factory=GenerationPolicy)
    advanced: AdvancedPolicy = Field(default_factory=AdvancedPolicy)
