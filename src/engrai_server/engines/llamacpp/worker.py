"""llama-server readiness semantics for the ENGRAI worker supervisor."""

from __future__ import annotations

from typing import Any

from ..worker import ProbeResult


class LlamaCppProbe:
    path = "/health"

    def inspect(self, status_code: int, payload: Any) -> ProbeResult:
        if status_code == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
            return ProbeResult(ready=True)
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict) and error.get("message"):
                return ProbeResult(ready=False, detail=str(error["message"]))
        return ProbeResult(ready=False, detail=f"llama-server health returned HTTP {status_code}")
