#!/usr/bin/env python3
"""Check release metadata against pyproject.toml before building artifacts."""

from __future__ import annotations

import argparse
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_version(root: Path = ROOT, tag: str = "") -> str:
    expected = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    versions = {
        "uv.lock": next(
            package["version"]
            for package in tomllib.loads((root / "uv.lock").read_text())["package"]
            if package["name"] == "engrai-server"
        ),
        "ui/package.json": json.loads((root / "ui/package.json").read_text())["version"],
    }
    lock = json.loads((root / "ui/package-lock.json").read_text())
    versions["ui/package-lock.json"] = lock["version"]
    versions["ui/package-lock.json (root package)"] = lock["packages"][""]["version"]
    container = re.search(r"^ARG OCI_VERSION=(\S+)$", (root / "Containerfile").read_text(), re.M)
    versions["Containerfile OCI_VERSION"] = container.group(1) if container else None
    errors = [f"{name}: {actual!r}, expected {expected!r}" for name, actual in versions.items() if actual != expected]
    if tag and tag != f"v{expected}":
        errors.append(f"release tag: {tag!r}, expected 'v{expected}'")
    if errors:
        raise ValueError("Version mismatch:\n" + "\n".join(errors))
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default="", help="Release tag, when building a tagged release")
    args = parser.parse_args()
    try:
        print(check_version(tag=args.tag))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
