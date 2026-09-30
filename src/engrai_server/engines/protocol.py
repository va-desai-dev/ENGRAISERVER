"""ENGRAI-owned boundary between orchestration and execution engines."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..domain.deployments import DeploymentProfile
from ..domain.models import ModelManifest


@dataclass(frozen=True, slots=True)
class EngineCapabilities:
    engine: str
    version: str | None
    backends: tuple[str, ...]
    features: frozenset[str]


@dataclass(frozen=True, slots=True)
class LaunchSpec:
    executable: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]

    def command(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)


class EngineAdapter(Protocol):
    id: str

    def capabilities(self) -> EngineCapabilities: ...

    def compile(
        self,
        manifest: ModelManifest,
        deployment: DeploymentProfile,
        artifacts: Mapping[str, Path],
        *,
        host: str,
        port: int,
    ) -> LaunchSpec: ...
