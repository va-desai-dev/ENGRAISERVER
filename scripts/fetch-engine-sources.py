#!/usr/bin/env python3
"""Fetch engine sources at the exact commits pinned in runtime/*.lock.json.

ENGRAI does not vendor engine sources or use Git submodules. Each lock file
names a repository and full commit (plus any submodules the build needs);
this script materializes them under third_party/<engine> and verifies every
revision. Re-running it is cheap when the checkouts already match.

    python3 scripts/fetch-engine-sources.py                  # all engines
    python3 scripts/fetch-engine-sources.py stable-diffusion.cpp
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCKS = {
    "llama.cpp": ROOT / "runtime" / "llama.cpp.lock.json",
    "stable-diffusion.cpp": ROOT / "runtime" / "stable-diffusion.cpp.lock.json",
}


def git(*arguments: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", *arguments), cwd=cwd, text=True, capture_output=True
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise SystemExit(f"git {' '.join(arguments)} failed in {cwd}:\n{detail}")
    return completed.stdout.strip()


def revision(path: Path) -> str | None:
    if not (path / ".git").exists():
        return None
    completed = subprocess.run(
        ("git", "rev-parse", "HEAD"), cwd=path, text=True, capture_output=True
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def checkout(path: Path, repository: str, commit: str) -> None:
    """Make ``path`` a shallow checkout of exactly ``commit``."""

    if revision(path) == commit:
        return
    if path.exists():
        if any(path.iterdir()) and not (path / ".git").exists():
            raise SystemExit(f"refusing to replace a non-Git directory: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("remote", "add", "origin", repository, cwd=path)
    git("fetch", "-q", "--depth", "1", "origin", commit, cwd=path)
    git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=path)
    if revision(path) != commit:
        raise SystemExit(f"{path} did not resolve to the pinned commit {commit}")


def submodule_url(source: Path, relative: str) -> str:
    return git(
        "config", "-f", ".gitmodules", f"submodule.{relative}.url", cwd=source
    )


def fetch(engine: str) -> None:
    lock = json.loads(LOCKS[engine].read_text(encoding="utf-8"))
    kernel = lock["kernel"]
    source = ROOT / "third_party" / engine
    checkout(source, kernel["repository"], kernel["commit"])
    for relative, commit in lock.get("submodules", {}).items():
        # The superproject records the submodule commit; the lock must agree,
        # so a lock edit cannot silently diverge from upstream's pin.
        recorded = git("ls-tree", "HEAD", relative, cwd=source).split()
        if len(recorded) < 3 or recorded[2] != commit:
            raise SystemExit(
                f"{engine} lock pins {relative} at {commit}, but the pinned "
                f"source records {recorded[2] if len(recorded) >= 3 else 'nothing'}"
            )
        checkout(source / relative, submodule_url(source, relative), commit)
    print(f"{engine}: {kernel['commit']} ({kernel.get('tag', 'untagged')})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("engines", nargs="*", metavar="engine", help=", ".join(LOCKS))
    args = parser.parse_args()
    unknown = sorted(set(args.engines) - LOCKS.keys())
    if unknown:
        parser.error(f"unknown engine: {', '.join(unknown)}")
    for engine in args.engines or LOCKS:
        fetch(engine)
    return 0


if __name__ == "__main__":
    sys.exit(main())
