"""Atomic XDG persistence for machine-local deployment profiles."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .deployments import DeploymentProfile


DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class DeploymentStore:
    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, deployment_id: str) -> Path:
        if not DEPLOYMENT_ID.fullmatch(deployment_id):
            raise ValueError("invalid deployment ID")
        path = (self.directory / f"{deployment_id}.json").resolve()
        if path.parent != self.directory:
            raise ValueError("deployment path escapes its XDG directory")
        return path

    def get(self, deployment_id: str) -> DeploymentProfile:
        path = self._path(deployment_id)
        if not path.is_file():
            raise KeyError(deployment_id)
        return DeploymentProfile.model_validate_json(path.read_text(encoding="utf-8"))

    def save(self, deployment: DeploymentProfile) -> DeploymentProfile:
        path = self._path(deployment.id)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(
            json.dumps(deployment.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        return self.get(deployment.id)

    def list(self) -> list[DeploymentProfile]:
        deployments: list[DeploymentProfile] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                deployments.append(self.get(path.stem))
            except (KeyError, OSError, ValueError):
                continue
        return deployments
