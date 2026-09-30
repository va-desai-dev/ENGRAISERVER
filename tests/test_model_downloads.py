from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from engrai_server.model_downloads import (
    DownloadError,
    ModelDownloadManager,
    ModelPullRequest,
)


def test_exact_selection_escapes_globs_and_supports_many_shards(tmp_path):
    import fnmatch
    names = [f"model-Q4_K_M-{index:05}-of-00040.gguf" for index in range(1, 41)]
    names.append("model[1]?.gguf")
    def snapshot(**kwargs):
        assert kwargs["ignore_patterns"] is None
        assert all(any(fnmatch.fnmatchcase(name, pattern) for pattern in kwargs["allow_patterns"]) for name in names)
        assert not any(fnmatch.fnmatchcase("model1x.gguf", pattern) for pattern in kwargs["allow_patterns"])
        return [dry_file(name) for name in names]
    manager = ModelDownloadManager(tmp_path / "hub", tmp_path / "state", snapshot_fn=snapshot)
    assert manager.preview(request(filenames=names, ignore_patterns=["*.gguf"]))["files_total"] == 41


def test_missing_exact_selection_does_not_silently_download_subset(tmp_path):
    manager = ModelDownloadManager(tmp_path / "hub", tmp_path / "state", snapshot_fn=lambda **_: [dry_file()])
    with pytest.raises(DownloadError, match="Selected files"):
        manager.preview(request(filenames=["example-Q4_K_M.gguf", "missing.gguf"]))


@pytest.mark.parametrize("names", [[], ["../bad.gguf"], ["/bad.gguf"], ["https://bad/file"], ["bad\\file"], ["bad\nfile"]])
def test_exact_selection_validates_paths(names):
    with pytest.raises(ValidationError):
        request(filenames=names)


def request(**changes) -> ModelPullRequest:
    return ModelPullRequest.model_validate(
        {
            "repo_id": "owner/example-GGUF",
            "revision": "main",
            "allow_patterns": ["*.gguf"],
            **changes,
        }
    )


def dry_file(
    filename: str = "example-Q4_K_M.gguf",
    *,
    size: int = 16,
    cached: bool = False,
):
    return SimpleNamespace(
        commit_hash="a" * 40,
        file_size=size,
        filename=filename,
        is_cached=cached,
        will_download=not cached,
    )


def test_preview_is_pinned_and_reports_required_storage(tmp_path: Path) -> None:
    seen = {}

    def fake_snapshot(**kwargs):
        seen.update(kwargs)
        return [dry_file(), dry_file("README.md", size=4, cached=True)]

    manager = ModelDownloadManager(
        tmp_path / "hub",
        tmp_path / "state.json",
        snapshot_fn=fake_snapshot,
    )
    preview = manager.preview(request(allow_patterns=["*.gguf", "README.md"]))

    assert preview["commit_hash"] == "a" * 40
    assert preview["bytes_total"] == 20
    assert preview["bytes_to_download"] == 16
    assert preview["destination"] == str((tmp_path / "hub").resolve())
    assert seen["dry_run"] is True
    assert seen["repo_type"] == "model"
    assert "local_dir" not in seen


@pytest.mark.parametrize(
    "payload",
    [
        {"repo_id": "no-namespace"},
        {"repo_id": "owner/repo", "allow_patterns": []},
        {"repo_id": "owner/repo", "allow_patterns": ["../secret"]},
        {"repo_id": "owner/repo", "allow_patterns": ["https://evil.invalid/file"]},
        {"repo_id": "owner/repo", "revision": "../../escape"},
    ],
)
def test_request_rejects_unbounded_or_unsafe_inputs(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ModelPullRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_pull_downloads_each_file_at_resolved_commit_and_persists(tmp_path: Path) -> None:
    cache = tmp_path / "hub"
    calls: list[dict] = []

    def fake_snapshot(**_kwargs):
        return [dry_file(), dry_file("metadata/config.json", size=4)]

    def fake_file(**kwargs):
        calls.append(kwargs)
        target = (
            cache
            / "models--owner--example-GGUF"
            / "snapshots"
            / ("a" * 40)
            / kwargs["filename"]
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"weights")
        return str(target)

    state = tmp_path / "downloads.json"
    manager = ModelDownloadManager(
        cache,
        state,
        snapshot_fn=fake_snapshot,
        file_fn=fake_file,
    )
    task = await manager.start(request(allow_patterns=["*.gguf", "*.json"]))

    for _ in range(100):
        record = next(item for item in manager.status()["tasks"] if item["id"] == task["id"])
        if record["status"] == "completed":
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("model pull did not complete")

    assert record["files_completed"] == 2
    assert record["bytes_completed"] == 20
    assert record["snapshot_path"].endswith(f"snapshots/{'a' * 40}")
    assert {call["revision"] for call in calls} == {"a" * 40}
    assert all(call["cache_dir"] == cache.resolve() for call in calls)
    persisted = json.loads(state.read_text(encoding="utf-8"))
    assert persisted["tasks"][0]["status"] == "completed"


def test_restart_marks_an_active_pull_interrupted(tmp_path: Path) -> None:
    state = tmp_path / "downloads.json"
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [{"id": "task", "status": "downloading", "created_at": 1}],
            }
        ),
        encoding="utf-8",
    )
    manager = ModelDownloadManager(tmp_path / "hub", state)
    record = manager.status()["tasks"][0]
    assert record["status"] == "interrupted"
    assert "restarted" in record["error"]
