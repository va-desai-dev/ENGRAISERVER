"""The ledger's embedding lane: a second, private llama-server.

It runs the same verified ENGRAI text runtime as the chat worker, started with
``--embedding`` on its own loopback port. Keeping it a separate process means a
model switch on the chat lane never drops the index's model, and an embedding
request never waits behind a generation.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from ..engines.llamacpp import LlamaCppProbe
from ..engines.protocol import LaunchSpec
from ..engines.worker import ManagedWorker, WorkerEndpoint, WorkerError
from ..runtime import RuntimeManagerError, inspect_runtime
from ..settings import Settings


BATCH = 16
# Longer inputs are cut before embedding; an entry's opening carries its subject.
MAX_INPUT_CHARS = 6000


UNKNOWN_ARCHITECTURE = re.compile(r"unknown model architecture: '([^']+)'")


class EmbeddingError(RuntimeError):
    """The embedding lane could not be started or did not answer."""


def explain_launch_failure(detail: str) -> str:
    """Say what went wrong in a sentence; the worker log keeps the rest."""

    unknown = UNKNOWN_ARCHITECTURE.search(detail)
    if unknown:
        return (
            f"This ENGRAI text runtime cannot load {unknown.group(1)!r} models. "
            "Choose an embedding model its llama.cpp supports, or install a newer runtime."
        )
    errors = [line for line in detail.splitlines() if " E " in line]
    reason = errors[0].split(" E ", 1)[1].strip() if errors else detail.splitlines()[0]
    return f"The embedding model did not load: {reason}"


def compile_embedding_launch(
    executable: Path,
    model_path: Path,
    *,
    host: str,
    port: int,
    gpu_layers: int,
) -> LaunchSpec:
    arguments = [
        "--model",
        str(model_path),
        "--alias",
        "engrai-embedding",
        "--host",
        host,
        "--port",
        str(port),
        "--offline",
        "--no-ui",
        "--embedding",
        "--parallel",
        "1",
        # An encoder processes its whole input in one physical batch, so the
        # default 512 would reject any entry longer than a paragraph or two.
        "--batch-size",
        "2048",
        "--ubatch-size",
        "2048",
        "--log-timestamps",
    ]
    if gpu_layers:
        arguments.extend(("--gpu-layers", str(gpu_layers)))
    else:
        arguments.extend(("--device", "none", "--gpu-layers", "0"))
    return LaunchSpec(executable, tuple(arguments), {})


class EmbeddingWorker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.worker = ManagedWorker(
            provider="llama.cpp-embedding",
            endpoint=WorkerEndpoint(settings.engine_host, settings.embedding_engine_port),
            probe=LlamaCppProbe(),
            log_path=settings.embedding_engine_log_path,
            state_path=settings.model_state_path.with_name("embedding-worker.json"),
            load_timeout_seconds=settings.model_load_timeout_seconds,
            stop_timeout_seconds=settings.engine_stop_timeout_seconds,
        )
        self.client = self.worker.client
        self.lock = asyncio.Lock()
        self.loaded: tuple[str, int] | None = None
        self.error: str | None = None

    def _executable(self) -> Path:
        try:
            status = inspect_runtime(self.settings.runtime_dir)
        except RuntimeManagerError as exc:
            raise EmbeddingError(str(exc)) from exc
        if not status["present"]:
            raise EmbeddingError("no ENGRAI text runtime is active; install a runtime bundle first")
        if not status["verified"]:
            raise EmbeddingError(f"active ENGRAI runtime failed verification: {status['error']}")
        return Path(str(status["executable"]))

    def _running(self) -> bool:
        process = self.worker.process
        return process is not None and process.returncode is None

    async def ensure(self, model_path: str, gpu_layers: int) -> None:
        wanted = (model_path, gpu_layers)
        if self.loaded == wanted and self._running():
            return
        async with self.lock:
            if self.loaded == wanted and self._running():
                return
            model = Path(model_path)
            if not model.is_file():
                raise EmbeddingError("the embedding model file is missing")
            executable = await asyncio.to_thread(self._executable)
            launch = compile_embedding_launch(
                executable,
                model,
                host=self.worker.endpoint.host,
                port=self.worker.endpoint.port,
                gpu_layers=gpu_layers,
            )
            self.loaded = None
            try:
                await self.worker.start("ledger", launch)
            except WorkerError as exc:
                self.error = explain_launch_failure(str(exc))
                raise EmbeddingError(self.error) from exc
            self.error = None
            self.loaded = wanted

    async def _request(self, texts: Sequence[str], timeout: float) -> list[list[float]]:
        try:
            response = await self.client.post(
                "/v1/embeddings", json={"input": list(texts)}, timeout=timeout
            )
            response.raise_for_status()
            rows = sorted(response.json()["data"], key=lambda row: row["index"])
            vectors = [[float(value) for value in row["embedding"]] for row in rows]
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise EmbeddingError(f"embedding request failed: {exc}") from exc
        if len(vectors) != len(texts):
            raise EmbeddingError("the embedding worker returned the wrong number of vectors")
        return vectors

    async def embed(
        self,
        texts: Sequence[str],
        *,
        model_path: str,
        gpu_layers: int,
        timeout: float = 120.0,
    ) -> list[list[float]]:
        await self.ensure(model_path, gpu_layers)
        clipped = [text[:MAX_INPUT_CHARS] for text in texts]
        vectors: list[list[float]] = []
        for start in range(0, len(clipped), BATCH):
            batch = clipped[start : start + BATCH]
            try:
                vectors.extend(await self._request(batch, timeout))
            except EmbeddingError:
                # One over-long input fails its whole batch. Models differ in
                # context, so retry each text alone, halving it until it fits.
                for text in batch:
                    vectors.append(await self._embed_one(text, timeout))
        return vectors

    async def _embed_one(self, text: str, timeout: float) -> list[float]:
        for _ in range(4):
            try:
                return (await self._request([text], timeout))[0]
            except EmbeddingError:
                if len(text) < 400:
                    raise
                text = text[: len(text) // 2]
        return (await self._request([text], timeout))[0]

    async def status(self) -> dict[str, Any]:
        running = self._running()
        return {
            "running": running,
            "model": self.loaded[0] if running and self.loaded else None,
            "error": None if running else self.error,
        }

    async def stop(self) -> None:
        self.loaded = None
        await self.worker.stop()

    async def close(self) -> None:
        self.loaded = None
        await self.worker.close()
