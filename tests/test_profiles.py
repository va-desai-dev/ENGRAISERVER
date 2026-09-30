from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrai_server.profiles import ProfileError, ProfileStore, infer_quantization


def test_route_round_trip(tmp_path: Path) -> None:
    model = tmp_path / "model-Q4_K_M.gguf"
    model.write_bytes(b"gguf")
    store = ProfileStore(tmp_path / "routes", [tmp_path])

    saved = store.save(
        "writer-31b",
        name="Writer",
        model_path=str(model),
        prompt={"template": "jinja", "adapter": "auto", "thinking": True},
        notes="Known split",
    )

    assert saved.routable is True
    assert saved.notes == "Known split"
    assert saved.prompt is not None and saved.prompt.thinking is True
    assert not (tmp_path / "routes" / "writer-31b.tmp").exists()
    payload = json.loads((tmp_path / "routes" / "writer-31b.json").read_text())
    assert payload["schema_version"] == 2
    assert "arguments" not in payload
    assert saved.public_dict()["quantization"] == "Q4_K_M"


@pytest.mark.parametrize("schema_version", [None, 1, 999])
def test_routes_other_than_schema_2_are_rejected(tmp_path: Path, schema_version: object) -> None:
    routes = tmp_path / "routes"
    routes.mkdir()
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    record: dict[str, object] = {"name": "Old", "model_path": str(model), "arguments": []}
    if schema_version is not None:
        record["schema_version"] = schema_version
    (routes / "old.json").write_text(json.dumps(record), encoding="utf-8")
    store = ProfileStore(routes, [tmp_path])

    with pytest.raises(ProfileError, match="unsupported schema_version"):
        store.get("old")
    assert store.list() == []


def test_profile_id_cannot_escape_directory(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    store = ProfileStore(tmp_path / "routes", [])
    with pytest.raises(ProfileError):
        store.save("../escape", name="Escape", model_path=str(model))


def test_route_requires_an_absolute_gguf_model(tmp_path: Path) -> None:
    store = ProfileStore(tmp_path / "routes", [])
    with pytest.raises(ProfileError, match="absolute"):
        store.save("relative", name="Relative", model_path="model.gguf")
    with pytest.raises(ProfileError, match=r"\.gguf"):
        store.save("weights", name="Weights", model_path=str(tmp_path / "model.bin"))


def test_missing_model_file_is_reported(tmp_path: Path) -> None:
    model = tmp_path / "model.gguf"
    model.write_bytes(b"gguf")
    store = ProfileStore(tmp_path / "routes", [])
    store.save("gone", name="Gone", model_path=str(model))
    model.unlink()

    route = store.get("gone")

    assert route.routable is False
    assert route.missing_paths == [model]
    with pytest.raises(ProfileError, match="readable GGUF"):
        store.resolve("gone")


def test_discovery_classifies_local_gguf_files(tmp_path: Path) -> None:
    (tmp_path / "text-Q5_K_M.gguf").write_bytes(b"text")
    (tmp_path / "mmproj-F16.gguf").write_bytes(b"vision")
    (tmp_path / "flux-Q4_K_M.gguf").write_bytes(b"image")
    store = ProfileStore(tmp_path / "routes", [tmp_path])

    by_name = {item["filename"]: item for item in store.discover_models()}

    assert by_name["text-Q5_K_M.gguf"]["kind"] == "text"
    assert by_name["mmproj-F16.gguf"]["kind"] == "projector"
    assert by_name["flux-Q4_K_M.gguf"]["kind"] == "image"


def test_quantization_is_inferred_from_filename() -> None:
    assert infer_quantization("Hermes-36B-Q5_K_M.gguf") == "Q5_K_M"
    assert infer_quantization("gemma-UD-Q4_K_XL.gguf") == "UD-Q4_K_XL"


def test_discovery_ignores_cache_blobs(tmp_path: Path) -> None:
    """Blobs are content-hashed with no extension; snapshots hold the names.

    Globbing *.gguf must therefore find each file once, via the snapshot.
    """
    repo = tmp_path / "models--org--repo"
    blobs = repo / "blobs"
    snapshot = repo / "snapshots" / "abc123"
    blobs.mkdir(parents=True)
    snapshot.mkdir(parents=True)
    blob = blobs / "e3b0c44298fc1c149afbf4c8996fb924"
    blob.write_bytes(b"gguf")
    (snapshot / "model-Q4_K_M.gguf").symlink_to(blob)

    store = ProfileStore(tmp_path / "routes", [tmp_path])
    found = store.discover_models()
    assert len(found) == 1
    assert found[0]["filename"] == "model-Q4_K_M.gguf"
