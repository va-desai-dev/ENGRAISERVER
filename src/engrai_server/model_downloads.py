"""Persistent, constrained Hugging Face model downloads.

The browser chooses a repository, revision, and file filters. It never chooses
the destination: keeping that server-owned closes the path-traversal and
arbitrary-overwrite class of bugs. Downloads use huggingface_hub directly, so
there is no shell or bundled CLI involved.
"""

from __future__ import annotations

import asyncio
import glob
import json
import os
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

from huggingface_hub import get_token, hf_hub_download, snapshot_download
from huggingface_hub.utils import tqdm as hf_tqdm
from huggingface_hub.utils import validate_repo_id
from pydantic import BaseModel, Field, field_validator


TERMINAL_STATES = {"completed", "failed", "cancelled", "interrupted"}
ACTIVE_STATES = {"queued", "resolving", "downloading", "cancelling"}


class QuietTqdm(hf_tqdm):
    """Keep library progress bars out of service logs; the API owns progress."""

    def __init__(self, *args: Any, **kwargs: Any):
        kwargs["disable"] = True
        super().__init__(*args, **kwargs)


class DownloadError(ValueError):
    pass


class DownloadBusyError(DownloadError):
    pass


class ModelPullRequest(BaseModel):
    repo_id: str = Field(min_length=3, max_length=128)
    revision: str = Field(default="main", min_length=1, max_length=200)
    allow_patterns: list[str] = Field(default_factory=lambda: ["*.gguf"], min_length=1, max_length=32)
    ignore_patterns: list[str] = Field(default_factory=list, max_length=32)
    force_download: bool = False
    filenames: list[str] | None = Field(default=None, min_length=1, max_length=4096)

    @field_validator("filenames")
    @classmethod
    def valid_filenames(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        for value in values:
            if (not value or len(value) > 1024 or value.startswith(("/", "\\"))
                    or "\\" in value or ".." in value.split("/") or ":" in value
                    or any(ord(char) < 32 for char in value)):
                raise ValueError("Unsafe repository filename")
        return list(dict.fromkeys(values))

    @field_validator("repo_id")
    @classmethod
    def valid_repo_id(cls, value: str) -> str:
        value = value.strip()
        validate_repo_id(value)
        if "/" not in value:
            raise ValueError("Model repository must be written as owner/name")
        return value

    @field_validator("revision")
    @classmethod
    def valid_revision(cls, value: str) -> str:
        value = value.strip()
        if (
            not value
            or value.startswith(("/", "\\"))
            or "\\" in value
            or ".." in PurePosixPath(value).parts
            or "://" in value
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("Revision contains invalid characters")
        return value

    @field_validator("allow_patterns", "ignore_patterns")
    @classmethod
    def valid_patterns(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for raw in values:
            value = raw.strip()
            parts = PurePosixPath(value).parts
            if (
                not value
                or len(value) > 200
                or value.startswith(("/", "\\"))
                or "\\" in value
                or ".." in parts
                or "://" in value
                or any(ord(character) < 32 for character in value)
            ):
                raise ValueError(f"Unsafe file pattern: {raw!r}")
            if value not in cleaned:
                cleaned.append(value)
        return cleaned


SnapshotDownload = Callable[..., Any]
FileDownload = Callable[..., Any]


class ModelDownloadManager:
    """Runs one model pull at a time and persists its public task history."""

    def __init__(
        self,
        cache_dir: Path,
        state_path: Path,
        *,
        snapshot_fn: SnapshotDownload = snapshot_download,
        file_fn: FileDownload = hf_hub_download,
    ):
        self.cache_dir = cache_dir.expanduser().resolve()
        self.state_path = state_path.expanduser().resolve()
        self._snapshot_fn = snapshot_fn
        self._file_fn = file_fn
        self._lock = threading.RLock()
        self._tasks = self._load()
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._interrupt_stale_tasks()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        records = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            return {}
        return {
            str(record["id"]): record
            for record in records
            if isinstance(record, dict) and isinstance(record.get("id"), str)
        }

    def _interrupt_stale_tasks(self) -> None:
        changed = False
        now = time.time()
        with self._lock:
            for record in self._tasks.values():
                if record.get("status") in ACTIVE_STATES:
                    record.update(
                        status="interrupted",
                        error="Gateway restarted before the download finished",
                        updated_at=now,
                    )
                    changed = True
            if changed:
                self._write()

    def _write(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        records = sorted(
            self._tasks.values(), key=lambda item: float(item.get("created_at", 0)), reverse=True
        )[:50]
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema_version": 1, "tasks": records}, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.state_path)

    @staticmethod
    def _request_kwargs(request: ModelPullRequest) -> dict[str, Any]:
        return {
            "repo_id": request.repo_id,
            "repo_type": "model",
            "revision": request.revision,
            "allow_patterns": ([glob.escape(name) for name in request.filenames]
                               if request.filenames else request.allow_patterns),
            "ignore_patterns": None if request.filenames else request.ignore_patterns or None,
            "force_download": request.force_download,
            "token": None,
        }

    def _resolve(self, request: ModelPullRequest) -> tuple[list[Any], dict[str, Any]]:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        result = self._snapshot_fn(
            **self._request_kwargs(request),
            cache_dir=self.cache_dir,
            tqdm_class=QuietTqdm,
            dry_run=True,
        )
        files = list(result)
        if request.filenames is not None and {str(item.filename) for item in files} != set(request.filenames):
            raise DownloadError("Selected files changed or are missing; inspect the repository again")
        if not files:
            raise DownloadError("No repository files matched the supplied filters")
        commit_hashes = {str(item.commit_hash) for item in files}
        if len(commit_hashes) != 1:
            raise DownloadError("Hugging Face returned files from more than one commit")
        total_bytes = sum(int(item.file_size or 0) for item in files)
        download_bytes = sum(
            int(item.file_size or 0) for item in files if bool(item.will_download)
        )
        free_bytes = shutil.disk_usage(self.cache_dir).free
        if download_bytes and free_bytes < download_bytes + 256 * 1024 * 1024:
            raise DownloadError(
                f"Not enough free storage: need {download_bytes} bytes plus a 256 MiB reserve"
            )
        preview = {
            "repo_id": request.repo_id,
            "requested_revision": request.revision,
            "commit_hash": commit_hashes.pop(),
            "files": [
                {
                    "filename": str(item.filename),
                    "size": int(item.file_size) if item.file_size is not None else None,
                    "cached": bool(item.is_cached),
                    "will_download": bool(item.will_download),
                }
                for item in files
            ],
            "files_total": len(files),
            "bytes_total": total_bytes,
            "bytes_to_download": download_bytes,
            "free_bytes": free_bytes,
        }
        return files, preview

    def preview(self, request: ModelPullRequest) -> dict[str, Any]:
        _, preview = self._resolve(request)
        return {**preview, "destination": str(self.cache_dir)}

    def status(self) -> dict[str, Any]:
        with self._lock:
            tasks = sorted(
                (dict(record) for record in self._tasks.values()),
                key=lambda item: float(item.get("created_at", 0)),
                reverse=True,
            )
        try:
            authenticated = bool(get_token())
        except Exception:
            authenticated = False
        return {
            "destination": str(self.cache_dir),
            "authenticated": authenticated,
            "active": next((item for item in tasks if item.get("status") in ACTIVE_STATES), None),
            "tasks": tasks[:20],
        }

    async def start(self, request: ModelPullRequest) -> dict[str, Any]:
        with self._lock:
            if any(record.get("status") in ACTIVE_STATES for record in self._tasks.values()):
                raise DownloadBusyError("Another model pull is already running")
            now = time.time()
            task_id = uuid.uuid4().hex
            record: dict[str, Any] = {
                "id": task_id,
                "repo_id": request.repo_id,
                "revision": request.revision,
                "allow_patterns": request.allow_patterns,
                "filenames": request.filenames,
                "ignore_patterns": request.ignore_patterns,
                "force_download": request.force_download,
                "status": "queued",
                "created_at": now,
                "updated_at": now,
                "commit_hash": None,
                "files_total": 0,
                "files_completed": 0,
                "bytes_total": 0,
                "bytes_completed": 0,
                "snapshot_path": None,
                "error": None,
                "cancel_requested": False,
            }
            self._tasks[task_id] = record
            self._write()
        runner = asyncio.create_task(self._run(task_id, request), name=f"hf-pull-{task_id}")
        self._runners[task_id] = runner
        runner.add_done_callback(lambda _: self._runners.pop(task_id, None))
        return dict(record)

    def _update(self, task_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._tasks[task_id]
            record.update(changes, updated_at=time.time())
            self._write()

    def _cancelled(self, task_id: str) -> bool:
        with self._lock:
            return bool(self._tasks[task_id].get("cancel_requested"))

    async def _run(self, task_id: str, request: ModelPullRequest) -> None:
        try:
            self._update(task_id, status="resolving")
            files, preview = await asyncio.to_thread(self._resolve, request)
            self._update(
                task_id,
                status="downloading",
                commit_hash=preview["commit_hash"],
                revision=preview["commit_hash"],
                files_total=preview["files_total"],
                bytes_total=preview["bytes_total"],
            )
            completed_bytes = 0
            downloaded: list[tuple[str, str]] = []
            for index, item in enumerate(files, start=1):
                if self._cancelled(task_id):
                    self._update(task_id, status="cancelled", error=None)
                    return
                local_path = await asyncio.to_thread(
                    self._file_fn,
                    repo_id=request.repo_id,
                    filename=str(item.filename),
                    repo_type="model",
                    revision=preview["commit_hash"],
                    cache_dir=self.cache_dir,
                    force_download=request.force_download,
                    token=None,
                )
                completed_bytes += int(item.file_size or 0)
                downloaded.append((str(item.filename), str(local_path)))
                self._update(
                    task_id,
                    files_completed=index,
                    bytes_completed=completed_bytes,
                )
            filename, local_path = downloaded[0]
            snapshot = Path(local_path)
            for _ in PurePosixPath(filename).parts:
                snapshot = snapshot.parent
            self._update(
                task_id,
                status="completed",
                snapshot_path=str(snapshot),
                error=None,
            )
        except asyncio.CancelledError:
            self._update(task_id, status="interrupted", error="Gateway stopped")
            raise
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            self._update(task_id, status="failed", error=message[:1000])

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError(task_id)
            if record.get("status") not in ACTIVE_STATES:
                raise DownloadError("That model pull is no longer running")
            record.update(
                cancel_requested=True,
                status="cancelling",
                updated_at=time.time(),
            )
            self._write()
            return dict(record)

    async def close(self) -> None:
        with self._lock:
            active = [
                task_id
                for task_id, record in self._tasks.items()
                if record.get("status") in ACTIVE_STATES
            ]
        for task_id in active:
            try:
                self.cancel(task_id)
            except DownloadError:
                pass
        # Do not cancel executor-backed file transfers mid-write. A container
        # stop may interrupt the current file, and huggingface_hub will safely
        # resume its .incomplete cache file on the next pull.
