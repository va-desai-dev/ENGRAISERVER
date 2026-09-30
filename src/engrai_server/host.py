"""Install and operate ENGRAI SERVER as a Linux user service.

The package owns executable code. XDG directories own mutable configuration,
runtime artifacts, and state. Nothing in this module assumes a clone, current
working directory, distribution package manager, or root privileges.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from .identity import SERVICE_NAME
from .settings import AppPaths, application_paths


class HostServiceError(RuntimeError):
    """The local host cannot install or operate the user service."""


def require_linux() -> None:
    if platform.system() != "Linux":
        raise HostServiceError(
            "systemd service management is currently supported on Linux only; "
            "run `engrai-server serve` in the foreground on this host"
        )


def systemd_user_dir() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(config_home).expanduser() if config_home else Path.home() / ".config"
    return root / "systemd" / "user"


def service_path() -> Path:
    return systemd_user_dir() / SERVICE_NAME


def default_environment() -> str:
    return """# ENGRAI SERVER host configuration.
# Paths default to the XDG locations shown by `engrai-server config`.
# Uncomment only settings this host needs to override.

ENGRAI_BIND_HOST=127.0.0.1
ENGRAI_BIND_PORT=8400
ENGRAI_FORWARDED_ALLOW_IPS=127.0.0.1

# ENGRAI_MODEL_SEARCH_ROOTS=/mnt/models:/home/me/.cache/huggingface/hub
# ENGRAI_MODEL_DOWNLOAD_DIR=/mnt/huggingface/hub
# HF_HOME=/mnt/huggingface
# HF_TOKEN=hf_scoped_read_token
# ENGRAI_TEXT_ENGINE_PORT=5002
# ENGRAI_IMAGE_ENGINE_PORT=5003
"""


def _atomic_write(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.chmod(mode)
    os.replace(temporary, path)


def initialize_layout(paths: AppPaths | None = None) -> AppPaths:
    paths = paths or application_paths()
    for directory in (
        paths.config,
        paths.config / "deployments",
        # Route and image profile directories are local configuration and are
        # never seeded from package data.
        paths.config / "routes",
        paths.config / "image-commands",
        paths.data,
        paths.data / "bin",
        paths.data / "runtimes",
        paths.state,
        paths.state / "image-results",
        paths.cache,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if not paths.environment.exists():
        _atomic_write(paths.environment, default_environment(), 0o600)
    return paths


def resolve_cli_executable(explicit: str | Path | None = None) -> Path:
    """Find a stable absolute console-script path for systemd."""

    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise HostServiceError(f"CLI executable is not runnable: {candidate}")

    invoked = Path(sys.argv[0]).expanduser()
    if invoked.name == "engrai-server":
        candidate = invoked.resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate

    if found := shutil.which("engrai-server"):
        return Path(found).resolve()
    raise HostServiceError(
        "Could not locate the installed engrai-server executable; install the package "
        "with `uv tool install .` (or from its Git URL) before installing the service"
    )


def _systemd_quote(value: Path) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_user_unit(
    executable: Path,
    environment: Path,
    portable_home: Path | None = None,
) -> str:
    home_line = (
        f"Environment=ENGRAI_HOME={_systemd_quote(portable_home)}\n"
        if portable_home is not None
        else ""
    )
    return f"""[Unit]
Description=ENGRAI SERVER host control plane
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{home_line}EnvironmentFile=-{environment}
ExecStart={_systemd_quote(executable)} serve
Restart=on-failure
RestartSec=3
TimeoutStopSec=35
UMask=0077

[Install]
WantedBy=default.target
"""


def _systemctl(*arguments: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("systemctl")
    if not executable:
        raise HostServiceError("systemctl is not installed; run `engrai-server serve` directly")
    try:
        return subprocess.run(
            [executable, "--user", *arguments],
            check=True,
            text=True,
            capture_output=capture,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise HostServiceError(f"systemctl --user {' '.join(arguments)} failed: {detail}") from exc


def install_user_service(
    *,
    executable: str | Path | None = None,
    enable: bool = True,
    start: bool = False,
) -> Path:
    require_linux()
    paths = initialize_layout()
    target = service_path()
    _atomic_write(
        target,
        render_user_unit(
            resolve_cli_executable(executable),
            paths.environment,
            paths.data if paths.portable else None,
        ),
        0o644,
    )
    _systemctl("daemon-reload")
    if enable:
        arguments = ["enable"]
        if start:
            arguments.append("--now")
        arguments.append(SERVICE_NAME)
        _systemctl(*arguments)
    elif start:
        _systemctl("start", SERVICE_NAME)
    return target


def service_action(action: str) -> subprocess.CompletedProcess[str]:
    require_linux()
    allowed = {"start", "stop", "restart", "status"}
    if action not in allowed:
        raise HostServiceError(f"Unsupported service action: {action}")
    return _systemctl(action, SERVICE_NAME, capture=action == "status")


def describe_paths(paths: AppPaths | None = None) -> dict[str, str]:
    paths = paths or application_paths()
    return {
        "layout": "portable" if paths.portable else "xdg",
        "config": str(paths.config),
        "data": str(paths.data),
        "state": str(paths.state),
        "cache": str(paths.cache),
        "environment": str(paths.environment),
        "service": str(service_path()),
    }
