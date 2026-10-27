from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_version", ROOT / "scripts/check-version.py")
assert spec and spec.loader
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def release_tree(tmp_path):
    for name in ("pyproject.toml", "uv.lock", "ui/package.json", "ui/package-lock.json", "Containerfile"):
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    return tmp_path


def test_release_versions_agree():
    version = checker.check_version()
    assert checker.check_version(tag=f"v{version}") == version


def test_release_tag_must_match_package_version():
    with pytest.raises(ValueError, match="release tag"):
        checker.check_version(tag="v99.0.0")


@pytest.mark.parametrize("name", ["uv.lock", "ui/package.json", "ui/package-lock.json", "Containerfile"])
def test_stale_release_metadata_is_rejected(release_tree, name):
    current = checker.check_version(release_tree)
    path = release_tree / name
    path.write_text(path.read_text().replace(current, "99.0.0"))
    with pytest.raises(ValueError, match="Version mismatch"):
        checker.check_version(release_tree)


def test_nested_npm_lock_version_is_checked(release_tree):
    path = release_tree / "ui/package-lock.json"
    lock = json.loads(path.read_text())
    lock["packages"][""]["version"] = "99.0.0"
    path.write_text(json.dumps(lock))
    with pytest.raises(ValueError, match="root package"):
        checker.check_version(release_tree)


def test_gateway_and_cli_report_package_version():
    from engrai_server.cli import version
    from engrai_server.main import app

    assert app.version == version() == checker.check_version()
