from __future__ import annotations

from pathlib import Path

import pytest

from engrai_server import host
from engrai_server.host import default_environment, initialize_layout, render_user_unit
from engrai_server.settings import AppPaths, Settings, application_paths


def clear_layout_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ENGRAI_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "CREDENTIALS_PATH",
        "PREFERENCES_PATH",
        "LLAMACPP_EXECUTABLE",
        "ENGRAI_LLAMACPP_EXECUTABLE",
        "ENGRAI_ROUTE_DIR",
        "ENGRAI_TEXT_ENGINE_LOG",
        "ENGRAI_IMAGE_COMMAND_DIR",
        "ENGRAI_IMAGE_ENGINE_LOG",
        "MODEL_STATE_PATH",
        "ENGRAI_MODEL_STATE_PATH",
        "IMAGE_STATE_PATH",
        "IMAGE_RESULT_DIR",
    ):
        monkeypatch.delenv(name, raising=False)


def test_default_settings_use_xdg_locations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_layout_environment(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))

    paths = application_paths()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert paths.config == tmp_path / "config" / "engrai-server"
    assert paths.environment == paths.config / "gateway.env"
    assert settings.route_dir == paths.config / "routes"
    assert settings.runtime_dir == paths.data / "runtimes"
    assert settings.llamacpp_executable == paths.data / "bin/llama-server"
    assert settings.credentials_path == paths.state / "credentials.json"
    assert settings.deployment_dir == paths.config / "deployments"
    assert settings.model_state_path == paths.state / "runtime.json"


def test_engrai_home_preserves_the_portable_checkout_layout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_layout_environment(monkeypatch)
    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))

    paths = application_paths()
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert paths.portable is True
    assert settings.runtime_dir == tmp_path / "runtimes"
    assert settings.llamacpp_executable == tmp_path / "bin/llama-server"
    assert settings.route_dir == tmp_path / "routes"
    assert settings.credentials_path == tmp_path / "state/credentials.json"


def test_relative_overrides_never_resolve_against_the_current_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_layout_environment(monkeypatch)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    settings = Settings(
        route_dir=Path("custom-routes"),
        llamacpp_executable=Path("runtime/llama-server"),
        credentials_path=Path("auth/credentials.json"),
        _env_file=None,
    )  # type: ignore[call-arg]

    assert settings.route_dir == tmp_path / "config/engrai-server/custom-routes"
    assert settings.llamacpp_executable == tmp_path / "data/engrai-server/runtime/llama-server"
    assert settings.credentials_path == tmp_path / "state/engrai-server/auth/credentials.json"


def test_initialize_layout_is_idempotent_and_keeps_existing_configuration(
    tmp_path: Path,
) -> None:
    paths = AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        state=tmp_path / "state",
        cache=tmp_path / "cache",
        environment=tmp_path / "config/gateway.env",
        portable=False,
    )
    initialize_layout(paths)
    assert paths.environment.read_text(encoding="utf-8") == default_environment()
    assert paths.environment.stat().st_mode & 0o777 == 0o600
    paths.environment.write_text("ENGRAI_BIND_PORT=9000\n", encoding="utf-8")

    initialize_layout(paths)

    assert paths.environment.read_text(encoding="utf-8") == "ENGRAI_BIND_PORT=9000\n"
    assert (paths.config / "deployments").is_dir()
    assert (paths.config / "routes").is_dir()
    assert (paths.config / "image-commands").is_dir()
    assert (paths.data / "bin").is_dir()
    assert (paths.data / "runtimes").is_dir()
    assert (paths.state / "image-results").is_dir()


def test_generated_service_uses_the_installed_cli_without_a_working_directory() -> None:
    unit = render_user_unit(
        Path("/home/test/.local/bin/engrai-server"),
        Path("/home/test/.config/engrai-server/gateway.env"),
    )

    assert 'ExecStart="/home/test/.local/bin/engrai-server" serve' in unit
    # systemd rejects a quoted EnvironmentFile path as "not absolute" and
    # silently skips the file.
    assert "EnvironmentFile=-/home/test/.config/engrai-server/gateway.env\n" in unit
    assert "WorkingDirectory=" not in unit
    assert "ENGRAI_HOME=" not in unit
    assert "WantedBy=default.target" in unit


def test_portable_service_records_its_explicit_home() -> None:
    unit = render_user_unit(
        Path("/opt/engrai/bin/engrai-server"),
        Path("/srv/engrai/.env"),
        Path("/srv/engrai"),
    )
    assert 'Environment=ENGRAI_HOME="/srv/engrai"' in unit


def test_service_install_writes_the_unit_and_runs_user_systemctl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    paths = AppPaths(
        config=tmp_path / "config",
        data=tmp_path / "data",
        state=tmp_path / "state",
        cache=tmp_path / "cache",
        environment=tmp_path / "config/gateway.env",
        portable=False,
    )
    executable = tmp_path / "bin/engrai-server"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    unit_path = tmp_path / "systemd/engrai-server.service"
    calls: list[tuple[str, ...]] = []

    monkeypatch.setattr(host, "require_linux", lambda: None)
    monkeypatch.setattr(host, "application_paths", lambda: paths)
    monkeypatch.setattr(host, "service_path", lambda: unit_path)
    monkeypatch.setattr(
        host,
        "_systemctl",
        lambda *arguments, **_kwargs: calls.append(arguments),
    )

    result = host.install_user_service(executable=executable, start=True)

    assert result == unit_path
    assert calls == [("daemon-reload",), ("enable", "--now", "engrai-server.service")]
    assert f'ExecStart="{executable}" serve' in unit_path.read_text(encoding="utf-8")
