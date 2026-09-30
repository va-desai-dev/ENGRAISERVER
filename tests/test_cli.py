from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrai_server.cli import OVERRIDES, apply_overrides, build_parser, main


@pytest.fixture(autouse=True)
def restore_override_env():
    """apply_overrides writes straight to os.environ, by design.

    monkeypatch cannot undo that: delenv on an already-unset variable records
    nothing, so a value written afterwards outlives the test and leaks into
    every later one.
    """
    import os

    saved = {name: os.environ.get(name) for name in OVERRIDES.values()}
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    from engrai_server.settings import get_settings

    get_settings.cache_clear()


def parse(argv: list[str]):
    return build_parser().parse_args(argv)


def test_host_and_port_are_accepted_by_serve() -> None:
    args = parse(["serve", "--host", "0.0.0.0", "--port", "9001"])
    assert args.host == "0.0.0.0"
    assert args.port == 9001


def test_overrides_are_accepted_by_every_subcommand() -> None:
    # config must take the same flags as serve, so a binding can be previewed
    # without starting anything.
    commands = ([name] for name in ("serve", "config", "preflight", "routes"))
    commands = [*commands, ["runtime", "status"]]
    for command in commands:
        args = parse([*command, "--host", "0.0.0.0", "--port", "9001"])
        assert args.host == "0.0.0.0"
        assert args.port == 9001


def test_runtime_install_options_are_parsed() -> None:
    args = parse(["runtime", "install", "bundle.tar.gz", "--force"])
    assert args.runtime_command == "install"
    assert args.bundle == "bundle.tar.gz"
    assert args.force is True


def test_linux_install_options_are_parsed() -> None:
    args = parse(["install", "--runtime-bundle", "bundle.tar.gz", "--now"])
    assert args.command == "install"
    assert args.runtime_bundle == "bundle.tar.gz"
    assert args.no_runtime is False
    assert args.now is True


def test_service_commands_are_parsed() -> None:
    install = parse(["service", "install", "--now"])
    status = parse(["service", "status"])
    assert install.service_command == "install"
    assert install.now is True
    assert status.service_command == "status"


def test_overrides_reach_settings_through_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENGRAI_BIND_HOST", raising=False)
    monkeypatch.delenv("ENGRAI_BIND_PORT", raising=False)
    apply_overrides(parse(["serve", "--host", "0.0.0.0", "--port", "9001"]))
    import os

    assert os.environ["ENGRAI_BIND_HOST"] == "0.0.0.0"
    assert os.environ["ENGRAI_BIND_PORT"] == "9001"


def test_absent_flags_leave_the_environment_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unset flag must not clobber a value from the env file.
    monkeypatch.setenv("ENGRAI_BIND_HOST", "192.168.1.5")
    apply_overrides(parse(["serve"]))
    import os

    assert os.environ["ENGRAI_BIND_HOST"] == "192.168.1.5"


def test_a_flag_beats_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENGRAI_BIND_HOST", "192.168.1.5")
    apply_overrides(parse(["serve", "--host", "0.0.0.0"]))
    import os

    assert os.environ["ENGRAI_BIND_HOST"] == "0.0.0.0"


def test_config_json_reports_the_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))
    assert main(["config", "--json", "--host", "0.0.0.0", "--port", "9100"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bind_host"] == "0.0.0.0"
    assert payload["bind_port"] == "9100"
    assert payload["runtime_dir"] == str(tmp_path / "runtimes")
    assert payload["primary_executable"] == "not installed"


def test_routes_json_on_an_empty_directory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))
    assert main(["routes", "--json", "--routes", str(tmp_path / "commands")]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_routes_json_lists_a_saved_profile(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("ENGRAI_HOME", str(tmp_path))
    model = tmp_path / "m-Q4_K_M.gguf"
    model.write_bytes(b"gguf")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "demo.json").write_text(
        json.dumps({"schema_version": 2, "name": "Demo", "model_path": str(model)}),
        encoding="utf-8",
    )
    assert main(["routes", "--json", "--routes", str(commands)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == [
        {"id": "demo", "name": "Demo", "routable": True, "model": str(model)}
    ]


def test_a_subcommand_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
