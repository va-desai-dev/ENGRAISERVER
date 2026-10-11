"""Generations that outlive the request that started them.

When an iPhone locks, Safari tears down the socket and an in-flight card is
lost. A request that opts in with ``resumable: true`` instead runs as a job:
the engine's output is buffered here unconditionally, forwarding to the
browser is best-effort, and a returning client replays what it missed by
character offset or collects the finished result.

Deliberately in memory. A generation lasts minutes at most, so a gateway
restart mid-flight is rare and the client treats it as an eviction.
"""

from __future__ import annotations

import asyncio
import codecs
import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

MAX_RUNNING = 8
MAX_CONTENT_CHARS = 1024 * 1024
RETENTION_SECONDS = 10 * 60
MAX_RUN_SECONDS = 20 * 60


class TooManyJobs(RuntimeError):
    pass


@dataclass(eq=False)
class Job:
    id: str
    client_ref: str | None
    status: str = "running"
    content: str = ""
    result: Any = None
    finish_reason: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    subscribers: int = 1
    listeners: set[asyncio.Queue] = field(default_factory=set)
    task: asyncio.Task | None = None

    def expired(self, now: float) -> bool:
        if self.status == "running":
            return now - self.created_at > MAX_RUN_SECONDS
        return now - (self.completed_at or self.created_at) > RETENTION_SECONDS

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "clientRef": self.client_ref,
            "status": self.status,
            "createdAt": int(self.created_at * 1000),
            "completedAt": int(self.completed_at * 1000) if self.completed_at else None,
            "length": len(self.content),
            "finishReason": self.finish_reason,
            "subscribers": self.subscribers,
        }


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def sweep(self) -> None:
        now = time.time()
        for job_id, job in list(self._jobs.items()):
            if not job.expired(now):
                continue
            if job.status == "running":
                # Over the hard cap: stop it, then keep the terminal record for
                # its retention window so a returning client learns why.
                self.finish(job, "error", error="Generation exceeded the maximum run time")
                if job.task and not job.task.done():
                    job.task.cancel()
                continue
            del self._jobs[job_id]

    def create(self, client_ref: object) -> Job:
        self.sweep()
        running = sum(1 for job in self._jobs.values() if job.status == "running")
        if running >= MAX_RUNNING:
            raise TooManyJobs(
                f"Too many generations in flight (limit {MAX_RUNNING}). Wait for one to finish."
            )
        job = Job(
            id=str(uuid.uuid4()),
            client_ref=client_ref[:100] if isinstance(client_ref, str) else None,
        )
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str) -> Job | None:
        self.sweep()
        return self._jobs.get(job_id)

    def list(self, client_ref: str | None = None) -> list[Job]:
        self.sweep()
        jobs = [j for j in self._jobs.values() if client_ref is None or j.client_ref == client_ref]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def _emit(self, job: Job, event: tuple[str, Any]) -> None:
        for queue in job.listeners:
            queue.put_nowait(event)

    def raw(self, job: Job, chunk: bytes) -> None:
        """Forward upstream bytes verbatim to the request that started the job."""
        self._emit(job, ("raw", chunk))

    def append(self, job: Job, text: str) -> bool:
        if job.status != "running" or not text:
            return True
        if len(job.content) + len(text) > MAX_CONTENT_CHARS:
            self.finish(
                job, "error",
                error=f"Generation exceeded the {MAX_CONTENT_CHARS} character buffer limit",
            )
            return False
        job.content += text
        self._emit(job, ("delta", text))
        return True

    def finish(
        self, job: Job, status: str, *, finish_reason: str | None = None, error: str | None = None
    ) -> None:
        if job.status != "running":
            return  # first terminal result wins
        job.status = status
        job.completed_at = time.time()
        if finish_reason:
            job.finish_reason = finish_reason
        if error:
            job.error = error
        self._emit(job, ("error" if status == "error" else "done", error or job.finish_reason))

    def subscribe(self, job: Job) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        job.listeners.add(queue)
        return queue

    def unsubscribe(self, job: Job, queue: asyncio.Queue) -> None:
        job.listeners.discard(queue)
        job.subscribers = max(0, job.subscribers - 1)


class SseContentExtractor:
    """Pull assembled text out of an OpenAI chat stream.

    Buffering by assembled content rather than raw bytes is what lets a resume
    offset survive different chunk boundaries between the original stream and
    a replay. Multi-byte characters can straddle chunks, hence the
    incremental decoder.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""

    def feed(self, chunk: bytes) -> Iterator[tuple[str, str]]:
        self._buffer += self._decoder.decode(chunk)
        *lines, self._buffer = self._buffer.split("\n")
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload.startswith("data:"):
                payload = payload[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                choice = (json.loads(payload).get("choices") or [{}])[0]
            except (ValueError, AttributeError, IndexError):
                continue  # keep-alives and non-JSON frames are not errors
            if not isinstance(choice, dict):
                continue
            content = (choice.get("delta") or {}).get("content")
            if isinstance(content, str) and content:
                yield "content", content
            if choice.get("finish_reason"):
                yield "finish", str(choice["finish_reason"])


def sse(payload: dict[str, Any]) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def sse_delta(text: str) -> bytes:
    return sse({"choices": [{"index": 0, "delta": {"content": text}}]})


def sse_finish(reason: str | None) -> bytes:
    return sse({"choices": [{"index": 0, "delta": {}, "finish_reason": reason or "stop"}]})


def sse_error(message: str) -> bytes:
    return sse({"error": {"message": message}})


SSE_DONE = b"data: [DONE]\n\n"
