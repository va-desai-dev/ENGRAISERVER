from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from engrai_server.credentials import CredentialError, CredentialStore
from engrai_server.throttle import LoginThrottle


SEED_KEY = "seed-api-key-aaaaaaaaaaaaaaaaaaaa"
SEED_ADMIN = "seed-admin-token-bbbbbbbbbbbb"


def store_at(tmp_path: Path, **overrides: str) -> CredentialStore:
    return CredentialStore(
        tmp_path / "credentials.json",
        seed_api_keys=overrides.get("seed_api_keys", SEED_KEY),
        seed_admin_token=overrides.get("seed_admin_token", SEED_ADMIN),
    )


def test_bootstraps_from_environment_seed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.verify_api_key(SEED_KEY) is True
    assert store.verify_admin_password(SEED_ADMIN) is True
    assert len(store.list_keys()) == 1


def test_store_is_written_private(tmp_path: Path) -> None:
    store_at(tmp_path)
    mode = (tmp_path / "credentials.json").stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_secrets_are_never_stored_in_plaintext(tmp_path: Path) -> None:
    store_at(tmp_path)
    raw = (tmp_path / "credentials.json").read_text(encoding="utf-8")
    assert SEED_KEY not in raw
    assert SEED_ADMIN not in raw


def test_existing_store_wins_over_seed(tmp_path: Path) -> None:
    store_at(tmp_path)
    # A second boot with different env values must not re-seed or overwrite.
    reopened = CredentialStore(
        tmp_path / "credentials.json",
        seed_api_keys="different-key",
        seed_admin_token="different-admin",
    )
    assert reopened.verify_api_key(SEED_KEY) is True
    assert reopened.verify_api_key("different-key") is False
    assert reopened.verify_admin_password(SEED_ADMIN) is True


def test_no_admin_token_leaves_the_store_uninitialised(tmp_path: Path) -> None:
    # Previously this raised. An unseeded install is now a first-run state that
    # the browser completes, rather than a startup failure.
    store = CredentialStore(tmp_path / "credentials.json", seed_api_keys=SEED_KEY)
    assert store.initialized is False


def test_bootstrap_requires_an_api_key(tmp_path: Path) -> None:
    with pytest.raises(CredentialError, match="at least one key"):
        CredentialStore(
            tmp_path / "credentials.json", seed_api_keys="", seed_admin_token=SEED_ADMIN
        )


def test_create_key_returns_plaintext_once(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    key, secret = store.create_key("Apple Shortcuts")
    assert secret.startswith("eng-")
    assert store.verify_api_key(secret) is True
    assert key.label == "Apple Shortcuts"
    assert key.prefix == secret[:12]
    # Only the hash is persisted.
    assert secret not in (tmp_path / "credentials.json").read_text(encoding="utf-8")


def test_revoke_key_removes_access(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    _, secret = store.create_key("temporary")
    key_id = store.list_keys()[-1].id
    store.revoke_key(key_id)
    assert store.verify_api_key(secret) is False
    assert store.verify_api_key(SEED_KEY) is True


def test_cannot_revoke_the_last_key(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    only_key = store.list_keys()[0].id
    with pytest.raises(CredentialError, match="last API key"):
        store.revoke_key(only_key)
    assert store.verify_api_key(SEED_KEY) is True


def test_revoke_unknown_key_raises_keyerror(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.create_key("second")
    with pytest.raises(KeyError):
        store.revoke_key("does-not-exist")


def test_change_password_requires_the_current_one(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(CredentialError, match="incorrect"):
        store.change_password("wrong", "a-new-long-password")
    assert store.verify_admin_password(SEED_ADMIN) is True


def test_change_password_enforces_a_minimum_length(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(CredentialError, match="at least 12"):
        store.change_password(SEED_ADMIN, "short")


def test_change_password_rejects_a_reused_password(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(CredentialError, match="must differ"):
        store.change_password(SEED_ADMIN, SEED_ADMIN)


def test_change_password_persists_and_rehashes(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.change_password(SEED_ADMIN, "a-brand-new-password")
    assert store.verify_admin_password("a-brand-new-password") is True
    assert store.verify_admin_password(SEED_ADMIN) is False

    payload = json.loads((tmp_path / "credentials.json").read_text(encoding="utf-8"))
    assert payload["admin"]["algorithm"] == "scrypt"
    assert "a-brand-new-password" not in json.dumps(payload)


def test_sessions_round_trip(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    token, expires_at = store.create_session()
    assert store.verify_session(token) is True
    assert expires_at > 0
    store.revoke_session(token)
    assert store.verify_session(token) is False


def test_expired_sessions_are_rejected(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    token, _ = store.create_session()
    store._sessions[token] = 0.0  # simulate expiry
    assert store.verify_session(token) is False


def test_changing_the_password_invalidates_every_session(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    token, _ = store.create_session()
    store.change_password(SEED_ADMIN, "a-brand-new-password")
    assert store.verify_session(token) is False


def test_empty_token_never_authenticates(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.verify_session("") is False
    assert store.verify_api_key("") is False
    assert store.verify_admin_password("") is False


def test_throttle_blocks_after_repeated_failures() -> None:
    throttle = LoginThrottle(max_failures=3, lockout_seconds=300)
    assert throttle.blocked_for("10.0.0.1") == 0
    for _ in range(3):
        throttle.record_failure("10.0.0.1")
    assert throttle.blocked_for("10.0.0.1") > 0
    # Other clients are unaffected.
    assert throttle.blocked_for("10.0.0.2") == 0


def test_throttle_resets_on_success() -> None:
    throttle = LoginThrottle(max_failures=2, lockout_seconds=300)
    throttle.record_failure("10.0.0.1")
    throttle.record_failure("10.0.0.1")
    assert throttle.blocked_for("10.0.0.1") > 0
    throttle.reset("10.0.0.1")
    assert throttle.blocked_for("10.0.0.1") == 0


def test_throttle_forgets_old_failures() -> None:
    throttle = LoginThrottle(max_failures=2, lockout_seconds=0)
    throttle.record_failure("10.0.0.1")
    throttle.record_failure("10.0.0.1")
    assert throttle.blocked_for("10.0.0.1") == 0


# --------------------------------------------------------------- onboarding


def test_uninitialised_store_is_a_valid_state(tmp_path: Path) -> None:
    # A fresh clone with no env seed must start, not crash, so the browser can
    # run setup.
    store = CredentialStore(tmp_path / "credentials.json")
    assert store.initialized is False
    assert store.list_keys() == []
    assert store.verify_api_key("anything") is False
    assert store.verify_admin_password("anything") is False
    assert not (tmp_path / "credentials.json").exists()


def test_initialize_creates_account_and_first_key(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    secret = store.initialize(
        name="Ada Lovelace", email="ada@example.com", password="a-long-enough-password"
    )
    assert store.initialized is True
    assert secret.startswith("eng-")
    assert store.verify_api_key(secret) is True
    assert store.verify_admin_password("a-long-enough-password") is True
    assert store.profile() == {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "initials": "AL",
    }
    raw = (tmp_path / "credentials.json").read_text(encoding="utf-8")
    assert secret not in raw and "a-long-enough-password" not in raw


def test_initialize_refuses_to_run_twice(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    store.initialize(name="First", email="", password="a-long-enough-password")
    with pytest.raises(CredentialError, match="Already initialised"):
        store.initialize(name="Attacker", email="", password="another-long-password")
    assert store.verify_admin_password("a-long-enough-password") is True


def test_initialize_enforces_password_length_and_name(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    with pytest.raises(CredentialError, match="at least 12"):
        store.initialize(name="Ada", email="", password="short")
    with pytest.raises(CredentialError, match="Name is required"):
        store.initialize(name="  ", email="", password="a-long-enough-password")
    assert store.initialized is False


def test_seeded_store_gets_a_default_profile(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    assert store.profile()["name"] == "Administrator"
    assert store.profile()["initials"] == "AD"


def test_update_profile_changes_the_monogram(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    profile = store.update_profile(name="Grace Hopper", email="grace@example.com")
    assert profile["initials"] == "GH"
    reopened = CredentialStore(tmp_path / "credentials.json")
    assert reopened.profile()["name"] == "Grace Hopper"


def test_update_profile_requires_a_name(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(CredentialError, match="Name is required"):
        store.update_profile(name="", email="x@example.com")


@pytest.mark.parametrize(
    ("name", "email", "expected"),
    [
        ("Grace Hopper", "", "GH"),
        ("madonna", "", "MA"),
        ("Ada Byron King", "", "AK"),
        ("", "vp@example.com", "VP"),
        ("", "", "EN"),
        ("  ", "7ada@example.com", "7A"),
    ],
)
def test_initials_fall_back_sensibly(name: str, email: str, expected: str) -> None:
    from engrai_server.credentials import derive_initials

    assert derive_initials(name, email) == expected
