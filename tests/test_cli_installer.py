from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import socket
import subprocess
import tarfile
import time
from pathlib import Path

import pytest
import httpx

INSTALLER = Path(__file__).resolve().parents[1] / "scripts/install.sh"


@pytest.fixture
def installation(tmp_path):
    commands = tmp_path / "commands"
    commands.mkdir()
    # Stub transport, not the installer. No network or real user profile writes.
    curl = commands / "curl"
    curl.write_text('''#!/bin/sh
printf '%s\\n' "$*" >> "$TEST_URLS"
case "$*" in *.sha256*) source="$TEST_DOWNLOAD.sha256" ;; *) source="$TEST_DOWNLOAD" ;; esac
while [ "$#" -gt 0 ]; do
  if [ "$1" = -o ]; then cp "$source" "$2"; exit; fi
  shift
done
exit 1
''')
    curl.chmod(0o755)
    for name, contents in {
        "uname": '#!/bin/sh\ncase "$1" in -s) echo "${TEST_SYSTEM:-Linux}" ;; -m) echo "${TEST_ARCH:-x86_64}" ;; esac\n',
        "sysctl": '#!/bin/sh\necho "${TEST_APPLE_ARM:-0}"\n',
    }.items():
        command = commands / name
        command.write_text(contents)
        command.chmod(0o755)
    home = tmp_path / "user's home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "SHELL": "/bin/bash",
           "PATH": f"{commands}:/usr/bin:/bin", "XDG_DATA_HOME": str(home / "data"),
           "ENGRAI_HOME": str(home / "state"), "TEST_DOWNLOAD": str(tmp_path / "download.tar.gz"),
           "TEST_URLS": str(tmp_path / "urls")}
    return tmp_path, home, env


def archive(env, *, missing_engine=False, broken=False, traversal=False):
    path = Path(env["TEST_DOWNLOAD"])
    with tarfile.open(path, "w:gz") as handle:
        executable = b"#!/bin/sh\nexit 1\n" if broken else b"#!/bin/sh\necho 'ENGRAI test bundle'\n"
        files = {"engrai-server/engrai-server": executable,
                 "engrai-server/runtimes/engrai-text/active.json": b"{}"}
        if not missing_engine:
            files["engrai-server/runtimes/engrai-image/active.json"] = b"{}"
        if traversal:
            files["engrai-server/../../escape"] = b"bad"
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name == "engrai-server/engrai-server" else 0o644
            handle.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_name(path.name + ".sha256").write_text(f"{digest}  bundle.tar.gz\n")


def run_installer(env, *arguments):
    return subprocess.run(["/bin/sh", str(INSTALLER), *arguments], env=env,
                          cwd="/", text=True, capture_output=True)


def test_install_from_anywhere_handles_spaces_and_quotes_and_preserves_state(installation):
    _, home, env = installation
    saved = home / "state/cards.json"
    saved.parent.mkdir()
    saved.write_text("saved card")
    archive(env)
    first = run_installer(env)
    assert first.returncode == 0, first.stderr
    launcher = home / ".local/bin/engrai-server"
    assert subprocess.check_output([str(launcher), "--version"], text=True).strip() == "ENGRAI test bundle"
    profile = (home / ".bashrc").read_text()
    second = run_installer(env)
    assert second.returncode == 0, second.stderr
    assert (home / ".bashrc").read_text() == profile
    assert saved.read_text() == "saved card"
    found = subprocess.check_output(["/bin/sh", "-c", '. "$HOME/.bashrc"; command -v engrai-server'], env=env, text=True)
    assert found.strip() == str(launcher)


@pytest.mark.parametrize("failure", ["checksum", "missing_engine", "broken", "traversal"])
def test_failed_update_leaves_previous_command_usable(installation, failure):
    _, home, env = installation
    archive(env)
    assert run_installer(env).returncode == 0
    launcher = home / ".local/bin/engrai-server"
    before = launcher.read_bytes()
    archive(env, **({failure: True} if failure != "checksum" else {}))
    if failure == "checksum":
        Path(env["TEST_DOWNLOAD"]).write_bytes(b"incomplete download")
    result = run_installer(env)
    assert result.returncode != 0
    assert launcher.read_bytes() == before
    assert not (home / "data/engrai-server/app/.install-lock").exists()
    assert subprocess.run([str(launcher), "--version"]).returncode == 0


def test_opt_out_does_not_modify_shell_profile(installation):
    _, home, env = installation
    archive(env)
    result = run_installer(env, "--no-modify-path")
    assert result.returncode == 0, result.stderr
    assert not (home / ".bashrc").exists()


@pytest.mark.parametrize("system,arch,apple_arm,target", [
    ("Linux", "x86_64", "0", "linux-x86_64-cpu"),
    ("Linux", "aarch64", "0", "linux-aarch64-cpu"),
    ("Darwin", "arm64", "1", "macos-aarch64-metal"),
    ("Darwin", "x86_64", "1", "macos-aarch64-metal"),  # Rosetta shell
    ("Darwin", "x86_64", "0", "macos-x86_64-cpu"),
])
def test_selects_platform_archive(installation, system, arch, apple_arm, target):
    _, _, env = installation
    env.update(TEST_SYSTEM=system, TEST_ARCH=arch, TEST_APPLE_ARM=apple_arm)
    archive(env)
    result = run_installer(env, "--version", "v0.1.0", "--no-modify-path")
    assert result.returncode == 0, result.stderr
    assert f"/download/v0.1.0/engrai-server-{target}.tar.gz" in Path(env["TEST_URLS"]).read_text()


@pytest.mark.skipif(not os.environ.get("ENGRAI_TEST_CLI_BUNDLE"), reason="build a complete CLI archive first")
def test_real_bundle_installs_and_reinstalls_without_toolchain(installation):
    root, home, env = installation
    source = Path(os.environ["ENGRAI_TEST_CLI_BUNDLE"])
    shutil.copyfile(source, env["TEST_DOWNLOAD"])
    shutil.copyfile(source.with_name(source.name + ".sha256"), env["TEST_DOWNLOAD"] + ".sha256")
    env.update(TEST_SYSTEM=platform.system(), TEST_ARCH=platform.machine())
    for name in ("python", "python3", "uv", "git", "cmake", "cc"):
        command = root / "commands" / name
        command.write_text('#!/bin/sh\necho "Unexpected developer tool: $0" >&2\nexit 99\n')
        command.chmod(0o755)
    launcher = home / ".local/bin/engrai-server"
    for first_run in (True, False):
        result = run_installer(env)
        assert result.returncode == 0, result.stdout + result.stderr
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with (root / "gateway.log").open("w+") as log:
            process = subprocess.Popen([str(launcher), "serve", "--port", str(port)],
                                       cwd="/", env=env, stdout=log, stderr=log)
            try:
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as client:
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        if process.poll() is not None:
                            log.seek(0)
                            pytest.fail(log.read())
                        try:
                            status = client.get("/control/status")
                            break
                        except httpx.TransportError:
                            time.sleep(0.1)
                    else:
                        pytest.fail("installed gateway did not start")
                    assert status.json() == {"initialized": not first_run}
                    assert client.get("/").status_code == 200
                    assert client.get("/cardgen/index.html").status_code == 200
                    login = {"password": "installer-test-password"}
                    if first_run:
                        login.update(name="Installer test", email="test@example.invalid")
                    response = client.post("/control/setup" if first_run else "/control/session", json=login)
                    assert response.status_code == 200, response.text
                    client.headers["Authorization"] = f"Bearer {response.json()['token']}"
                    if first_run:
                        saved = client.post("/cardgen/api/storage/cards", json={"character": {"name": "Preserved card"}})
                        assert saved.status_code == 200, saved.text
                    cards = client.get("/cardgen/api/storage/cards").json()
                    assert len(cards) == 1
                    assert cards[0]["characterName"] == "Preserved card"
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
