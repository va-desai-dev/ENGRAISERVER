"""Compile resolved ENGRAI deployments into deterministic llama-server launches.

The adapter intentionally refuses unresolved ``auto`` choices. Hardware
discovery and planning happen above this layer; launching a worker must never
silently change context size or GPU placement to make a configuration fit.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from pathlib import Path

from ...domain.deployments import DeploymentProfile
from ...domain.models import ModelManifest
from ..protocol import EngineCapabilities, LaunchSpec


_CONTROLLED_ARGUMENTS = frozenset(
    {
        "-a",
        "-ag",
        "-c",
        "-ctk",
        "-ctv",
        "-dev",
        "-fa",
        "-fit",
        "-mg",
        "-m",
        "-n",
        "-ngl",
        "-np",
        "-sm",
        "-ts",
        "--alias",
        "--agent",
        "--api-prefix",
        "--api-key",
        "--api-key-file",
        "--cache-prompt",
        "--cache-reuse",
        "--cache-type-k",
        "--cache-type-v",
        "--chat-template",
        "--context-shift",
        "--cors-credentials",
        "--cors-headers",
        "--cors-methods",
        "--cors-origins",
        "--ctx-size",
        "--device",
        "--embedding",
        "--fit",
        "--flash-attn",
        "--gpu-layers",
        "--host",
        "--hf-file",
        "--hf-repo",
        "--hf-token",
        "--jinja",
        "--kv-offload",
        "--log-jsonl",
        "--log-timestamps",
        "--main-gpu",
        "--mcp-servers-config",
        "--mcp-servers-json",
        "--media-path",
        "--metrics",
        "--mmproj",
        "--mmproj-url",
        "--model",
        "--model-url",
        "--models-dir",
        "--models-preset",
        "--n-gpu-layers",
        "--n-predict",
        "--no-cache-prompt",
        "--no-context-shift",
        "--no-jinja",
        "--no-kv-offload",
        "--no-metrics",
        "--no-models-autoload",
        "--no-slots",
        "--no-ui",
        "--no-ui-mcp-proxy",
        "--offline",
        "--parallel",
        "--path",
        "--perf",
        "--port",
        "--props",
        "--reasoning",
        "--reasoning-effort",
        "--rerank",
        "--rpc",
        "--slots",
        "--slot-save-path",
        "--sleep-idle-seconds",
        "--split-mode",
        "--ssl-cert-file",
        "--ssl-key-file",
        "--tensor-split",
        "--tools",
        "--tools-runtime",
        "--ui",
        "--ui-mcp-proxy",
        "--webui",
    }
)


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_extra_arguments(arguments: list[str]) -> None:
    """Reject escape-hatch flags whose semantics ENGRAI models explicitly."""

    for token in arguments:
        name = token.split("=", 1)[0]
        if name in _CONTROLLED_ARGUMENTS:
            raise ValueError(f"advanced arguments cannot override ENGRAI-owned option {name}")


class LlamaCppAdapter:
    """Strict compiler for one probed llama-server build and device inventory."""

    id = "llama.cpp"

    def __init__(
        self,
        executable: Path,
        *,
        backend: str,
        devices: Mapping[int, str] | None = None,
        version: str | None = None,
    ) -> None:
        if backend not in {"cuda", "rocm", "metal", "vulkan", "cpu"}:
            raise ValueError("llama.cpp backend must be resolved before compilation")
        self.executable = executable
        self.backend = backend
        self.devices = dict(devices or {})
        self.version = version

    def capabilities(self) -> EngineCapabilities:
        backends = ("cpu",) if self.backend == "cpu" else (self.backend, "cpu")
        return EngineCapabilities(
            engine=self.id,
            version=self.version,
            backends=backends,
            features=frozenset(
                {
                    "chat",
                    "completion",
                    "jinja",
                    "tensor_split",
                    "flash_attention",
                    "quantized_kv",
                    "prompt_cache",
                    "cache_reuse",
                    "context_shift",
                    "metrics",
                    "slots",
                }
            ),
        )

    def compile(
        self,
        manifest: ModelManifest,
        deployment: DeploymentProfile,
        artifacts: Mapping[str, Path],
        *,
        host: str,
        port: int,
    ) -> LaunchSpec:
        if deployment.engine != self.id:
            raise ValueError(f"deployment engine must be {self.id!r}")
        if deployment.engine_version and deployment.engine_version != self.version:
            observed = self.version or "unknown"
            raise ValueError(
                f"deployment requires llama.cpp {deployment.engine_version}, "
                f"but the selected runtime is {observed}"
            )
        if manifest.format != "gguf":
            raise ValueError("the llama.cpp worker requires a GGUF model")
        if manifest.task not in {"chat", "completion"} or manifest.capabilities.vision:
            raise ValueError(
                "the initial direct llama.cpp worker supports text chat and completion only"
            )
        if not _is_loopback(host):
            raise ValueError("engine workers must bind to a loopback address")
        if not 1 <= port <= 65535:
            raise ValueError("engine worker port must be between 1 and 65535")
        try:
            model_path = artifacts["model"]
        except KeyError as exc:
            raise ValueError("resolved artifacts must include the model role") from exc
        if not model_path.is_absolute():
            raise ValueError("resolved artifact paths must be absolute")

        unsupported = set(manifest.required_engine_capabilities) - self.capabilities().features
        if unsupported:
            raise ValueError(
                "llama.cpp worker lacks required capabilities: "
                + ", ".join(sorted(unsupported))
            )

        compute = deployment.compute
        memory = deployment.memory
        generation = deployment.generation
        if compute.backend == "auto":
            raise ValueError("compute backend is unresolved; run the deployment planner first")
        if compute.backend != "cpu" and compute.backend != self.backend:
            raise ValueError(
                f"deployment requests {compute.backend}, but this llama.cpp build is {self.backend}"
            )
        if compute.gpu_layers == "auto":
            raise ValueError("GPU layer count is unresolved; run the deployment planner first")

        selected_devices: list[int] = []
        if compute.backend != "cpu":
            selected_devices = list(compute.devices)
            if not selected_devices:
                if len(self.devices) != 1:
                    raise ValueError(
                        "device selection is ambiguous; the planner must choose accelerator devices"
                    )
                selected_devices = list(self.devices)
            missing = [index for index in selected_devices if index not in self.devices]
            if missing:
                raise ValueError(
                    "selected devices were not reported by llama-server: "
                    + ", ".join(str(index) for index in missing)
                )
            if len(selected_devices) > 1 and not compute.tensor_split:
                raise ValueError("multi-GPU deployments require an explicit tensor_split")
            if compute.tensor_split and len(compute.tensor_split) != len(selected_devices):
                raise ValueError("tensor_split must have one value per resolved device")
            if compute.main_gpu is not None and compute.main_gpu not in selected_devices:
                raise ValueError("main_gpu must be one of the resolved devices")
            if compute.main_gpu is not None and compute.split_mode == "layer":
                raise ValueError("main_gpu is meaningful only with split_mode 'none' or 'row'")

        context = memory.context or manifest.limits.recommended_context
        if context is None:
            raise ValueError("context size is unresolved; set it in the deployment profile")
        if manifest.limits.maximum_context and context > manifest.limits.maximum_context:
            raise ValueError(
                f"context {context} exceeds the manifest maximum "
                f"{manifest.limits.maximum_context}"
            )

        validate_extra_arguments(deployment.advanced.extra_arguments)
        arguments = [
            "--model",
            str(model_path),
            "--alias",
            deployment.id,
            "--host",
            host,
            "--port",
            str(port),
            "--offline",
            "--no-ui",
            "--metrics",
            "--slots",
            "--perf",
            "--log-jsonl",
            "--log-timestamps",
            "--parallel",
            str(memory.parallel_slots),
            "--cache-prompt",
            "--cache-reuse",
            str(memory.cache_reuse_min_tokens),
            "--context-shift" if memory.context_shift else "--no-context-shift",
            "--ctx-size",
            str(context),
            "--cache-type-k",
            memory.kv_cache,
            "--cache-type-v",
            memory.kv_cache,
            "--kv-offload" if memory.kv_offload else "--no-kv-offload",
            "--flash-attn",
            "on" if compute.flash_attention else "off",
            # llama.cpp's fit mode may reduce context or alter placement. A
            # launch spec is already planned, so mutation here is forbidden.
            "--fit",
            "off",
        ]

        if compute.backend == "cpu":
            if compute.devices or compute.tensor_split or compute.main_gpu is not None:
                raise ValueError("CPU deployments cannot select accelerator placement")
            arguments.extend(("--device", "none", "--gpu-layers", "0"))
        else:
            arguments.extend(
                (
                    "--device",
                    ",".join(self.devices[index] for index in selected_devices),
                    "--gpu-layers",
                    str(compute.gpu_layers),
                    "--split-mode",
                    compute.split_mode,
                )
            )
            if compute.tensor_split:
                arguments.extend(
                    ("--tensor-split", ",".join(str(value) for value in compute.tensor_split))
                )
            if compute.main_gpu is not None:
                # llama.cpp interprets main-gpu as an ordinal in the filtered
                # --device list. Deployment profiles use stable host indices.
                arguments.extend(
                    ("--main-gpu", str(selected_devices.index(compute.main_gpu)))
                )

        if manifest.prompt:
            if manifest.prompt.template == "none":
                arguments.append("--no-jinja")
            else:
                arguments.append("--jinja")
            if manifest.prompt.adapter != "auto":
                arguments.extend(("--chat-template", manifest.prompt.adapter))
            if manifest.prompt.thinking:
                arguments.extend(("--reasoning", "on"))
        if generation.default_tokens is not None:
            arguments.extend(("--n-predict", str(generation.default_tokens)))
        if generation.reasoning_effort is not None:
            arguments.extend(("--reasoning-effort", generation.reasoning_effort))

        arguments.extend(deployment.advanced.extra_arguments)
        return LaunchSpec(self.executable, tuple(arguments), {})
