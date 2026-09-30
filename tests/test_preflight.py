from __future__ import annotations

import json
from pathlib import Path

import pytest

from engrai_server.preflight import (
    check_credentials,
    check_hf_library,
    check_text_runtime,
    check_model_cache,
    check_profile_fit,
    check_profiles,
    parse_hf_reference,
    split_siblings,
)
from engrai_server.settings import Settings, default_hf_cache


HF_PATH = (
    "/home/user/.cache/huggingface/hub/"
    "models--exampleorg--Example-31B-GGUF/"
    "snapshots/eee61b81461ac75eb920a24ca9e5d420bb66e33d/"
    "Example-31B-Q4_K_M.gguf"
)


def test_parse_hf_reference_recovers_repo_file_and_revision() -> None:
    reference = parse_hf_reference(HF_PATH)
    assert reference is not None
    assert reference.repo_id == "exampleorg/Example-31B-GGUF"
    assert reference.filename == "Example-31B-Q4_K_M.gguf"
    assert reference.revision == "eee61b81461ac75eb920a24ca9e5d420bb66e33d"


def test_parse_hf_reference_handles_nested_filenames() -> None:
    # Multi-part quants live in a subdirectory of the snapshot.
    reference = parse_hf_reference(
        "/cache/hub/models--org--repo/snapshots/abc123/Q4_K_M/model-00001-of-00002.gguf"
    )
    assert reference is not None
    assert reference.filename == "Q4_K_M/model-00001-of-00002.gguf"


def test_parse_hf_reference_rejects_non_cache_paths() -> None:
    assert parse_hf_reference("/mnt/models/local-model.gguf") is None


def settings_for(tmp_path: Path, **overrides: object) -> Settings:
    defaults = {
        "control_api_keys": "test-key",
        "control_admin_token": "test-admin",
        "runtime_dir": tmp_path / "runtimes",
        "route_dir": tmp_path / "commands",
        "text_engine_log_path": tmp_path / "state/text-worker.log",
        "model_state_path": tmp_path / "state/runtime.json",
        "model_search_roots": str(tmp_path),
        "model_download_dir": tmp_path,
    }
    return Settings(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_check_text_runtime_reports_missing_bundle(tmp_path: Path) -> None:
    check = check_text_runtime(settings_for(tmp_path))
    assert check.status == "fail"
    assert "runtime install" in check.remedy


def test_check_text_runtime_rejects_a_corrupt_active_bundle(tmp_path: Path) -> None:
    runtime = tmp_path / "runtimes" / "engrai-text" / "bad"
    runtime.mkdir(parents=True)
    (tmp_path / "runtimes" / "active.json").write_text(
        json.dumps({"path": "engrai-text/bad"}), encoding="utf-8"
    )
    check = check_text_runtime(
        settings_for(tmp_path, runtime_dir=tmp_path / "runtimes")
    )
    assert check.status == "fail"
    assert "engrai-runtime.json" in check.detail


def test_check_profiles_flags_missing_weights(tmp_path: Path) -> None:
    model = tmp_path / "present-Q4_K_M.gguf"
    model.write_bytes(b"gguf")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "here.json").write_text(
        f'{{"name": "Here", "model_path": "{model}", "schema_version": 2}}', encoding="utf-8"
    )
    (commands / "gone.json").write_text(
        f'{{"name": "Gone", "model_path": "{tmp_path / "gone.gguf"}", "schema_version": 2}}',
        encoding="utf-8",
    )

    check = check_profiles(settings_for(tmp_path))
    assert check.status == "fail"
    assert "gone" in check.detail
    assert "1/2 routable" in check.detail


@pytest.fixture
def clean_hf_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HF_HUB_CACHE", "HF_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)


def test_hf_cache_defaults_to_home(clean_hf_env: None) -> None:
    assert default_hf_cache() == Path.home() / ".cache" / "huggingface" / "hub"


def test_hf_cache_honours_hf_home(
    clean_hf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", "/mnt/bulk/hf")
    assert default_hf_cache() == Path("/mnt/bulk/hf/hub")


def test_hf_cache_honours_xdg(clean_hf_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CACHE_HOME", "/mnt/bulk/cache")
    assert default_hf_cache() == Path("/mnt/bulk/cache/huggingface/hub")


def test_hf_cache_hub_cache_wins(
    clean_hf_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", "/mnt/ignored")
    monkeypatch.setenv("XDG_CACHE_HOME", "/mnt/also-ignored")
    monkeypatch.setenv("HF_HUB_CACHE", "/mnt/bulk/hub")
    assert default_hf_cache() == Path("/mnt/bulk/hub")


def test_check_model_cache_counts_weights(tmp_path: Path) -> None:
    (tmp_path / "a-Q4_K_M.gguf").write_bytes(b"gguf")
    (tmp_path / "b-Q5_K_M.gguf").write_bytes(b"gguf")
    check = check_model_cache(settings_for(tmp_path))
    assert check.status == "ok"
    assert "2 GGUF file(s)" in check.detail


def test_check_model_cache_warns_on_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "nowhere"
    check = check_model_cache(
        settings_for(
            tmp_path,
            model_search_roots=str(missing),
            model_download_dir=missing,
        )
    )
    assert check.status == "warn"
    assert "No search root exists" in check.detail


def test_check_profiles_passes_when_all_routable(tmp_path: Path) -> None:
    model = tmp_path / "present-Q4_K_M.gguf"
    model.write_bytes(b"gguf")
    commands = tmp_path / "commands"
    commands.mkdir()
    (commands / "here.json").write_text(
        f'{{"name": "Here", "model_path": "{model}", "schema_version": 2}}', encoding="utf-8"
    )

    check = check_profiles(settings_for(tmp_path))
    assert check.status == "ok"


def test_bind_defaults_to_loopback(tmp_path: Path) -> None:
    # A fresh install must not expose an admin surface to the network.
    settings = settings_for(tmp_path)
    assert settings.bind_host == "127.0.0.1"
    assert settings.bind_port == 8400
    assert settings.forwarded_peers == ["127.0.0.1"]


def test_bind_host_is_configurable(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, bind_host="0.0.0.0", bind_port=9001)
    assert settings.bind_host == "0.0.0.0"
    assert settings.bind_port == 9001


def test_forwarded_peers_accepts_a_wildcard(tmp_path: Path) -> None:
    # uvicorn treats "*" as a string sentinel, not a one-element list.
    assert settings_for(tmp_path, forwarded_allow_ips="*").forwarded_peers == "*"


def test_forwarded_peers_splits_a_list(tmp_path: Path) -> None:
    settings = settings_for(tmp_path, forwarded_allow_ips="127.0.0.1, 10.0.0.5")
    assert settings.forwarded_peers == ["127.0.0.1", "10.0.0.5"]


def test_split_gguf_expands_to_every_part() -> None:
    # A split model is unusable unless all parts are present, so a restore
    # driven by the profile's single path must fetch the siblings too.
    parts = split_siblings(
        "UD-Q2_K_XL/DeepSeek-V4-Flash-Vision-Exp-UD-Q2_K_XL-00001-of-00003.gguf"
    )
    assert parts == [
        "UD-Q2_K_XL/DeepSeek-V4-Flash-Vision-Exp-UD-Q2_K_XL-00001-of-00003.gguf",
        "UD-Q2_K_XL/DeepSeek-V4-Flash-Vision-Exp-UD-Q2_K_XL-00002-of-00003.gguf",
        "UD-Q2_K_XL/DeepSeek-V4-Flash-Vision-Exp-UD-Q2_K_XL-00003-of-00003.gguf",
    ]


def test_split_expansion_starts_from_any_part() -> None:
    # The profile may name part 2 rather than part 1.
    assert len(split_siblings("model-00002-of-00004.gguf")) == 4
    assert split_siblings("model-00002-of-00004.gguf")[0] == "model-00001-of-00004.gguf"


def test_split_expansion_preserves_zero_padding() -> None:
    assert split_siblings("m-1-of-2.gguf") == ["m-1-of-2.gguf", "m-2-of-2.gguf"]


def test_unsplit_gguf_is_returned_alone() -> None:
    assert split_siblings("gemma-4-31B-it-qat-UD-Q4_K_XL.gguf") == [
        "gemma-4-31B-it-qat-UD-Q4_K_XL.gguf"
    ]


def test_hf_python_library_is_the_integrated_dependency() -> None:
    check = check_hf_library()
    assert check.status == "ok"
    assert "in-process" in check.detail


def test_credentials_check_reports_no_account_on_a_bare_clone(tmp_path: Path) -> None:
    # Reading the seed variables alone once claimed "admin token set" on a
    # clone that had no account at all.
    settings = settings_for(
        tmp_path,
        control_api_keys="",
        control_admin_token="",
        credentials_path=tmp_path / "credentials.json",
    )
    check = check_credentials(settings)
    assert check.status == "warn"
    assert "no account yet" in check.detail
    # A check must never create the thing it is checking for.
    assert not (tmp_path / "credentials.json").exists()


def test_credentials_check_notes_a_pending_seed(tmp_path: Path) -> None:
    settings = settings_for(
        tmp_path,
        control_admin_token="seed-token",
        credentials_path=tmp_path / "credentials.json",
    )
    check = check_credentials(settings)
    assert check.status == "ok"
    assert "ENGRAI_ADMIN_TOKEN" in check.detail
    assert not (tmp_path / "credentials.json").exists()


def test_credentials_check_reads_the_store_once_it_exists(tmp_path: Path) -> None:
    from engrai_server.credentials import CredentialStore

    store = CredentialStore(tmp_path / "credentials.json")
    store.initialize(name="Ada Lovelace", email="", password="a-long-enough-password")
    settings = settings_for(tmp_path, credentials_path=tmp_path / "credentials.json")
    check = check_credentials(settings)
    assert check.status == "ok"
    assert "1 API key(s)" in check.detail
    assert "Ada Lovelace" in check.detail


def test_profile_fit_does_not_pass_vacuously(tmp_path: Path) -> None:
    check = check_profile_fit(settings_for(tmp_path))
    assert "no routes yet" in check.detail or check.status == "warn"


def test_gateway_env_example_ships_no_live_credentials() -> None:
    """restore.sh copies this file into place on a fresh machine.

    Any uncommented CONTROL_* line would seed a real install with a credential
    published in this repository.
    """
    text = (Path(__file__).resolve().parent.parent / "deploy/gateway.env.example").read_text(
        encoding="utf-8"
    )
    active = [
        line
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert not [line for line in active if line.startswith(("CONTROL_", "ENGRAI_API_KEYS", "ENGRAI_ADMIN_TOKEN"))], (
        f"gateway.env.example must not ship live credentials: {active}"
    )
    # The listen address is the one thing an installer is expected to set.
    assert any(line.startswith("ENGRAI_BIND_HOST=") for line in active)


def test_service_unit_tolerates_a_missing_env_file() -> None:
    """systemd refuses to start a unit whose EnvironmentFile is absent.

    That is the state of every fresh machine, so the reference must be
    optional.
    """
    unit = (Path(__file__).resolve().parent.parent / "deploy/engrai-server.service").read_text(
        encoding="utf-8"
    )
    lines = [line for line in unit.splitlines() if line.startswith("EnvironmentFile=")]
    assert lines, "unit no longer references an environment file"
    for line in lines:
        assert line.startswith("EnvironmentFile=-"), f"not optional: {line}"
