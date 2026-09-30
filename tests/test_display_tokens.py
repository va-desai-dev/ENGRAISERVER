"""Scope rules for the wall-display credential.

A display token exists so a monitor bolted to a wall does not need a password
after every power cut. That convenience is only acceptable because the token
is narrow, so the boundaries are asserted here rather than assumed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engrai_server.credentials import CredentialError, CredentialStore


@pytest.fixture
def store(tmp_path: Path) -> CredentialStore:
    return CredentialStore(
        tmp_path / "credentials.json",
        seed_api_keys="seed-key-0000000000",
        seed_admin_token="a-long-enough-password",
    )


def test_a_display_token_is_not_an_api_key(store: CredentialStore) -> None:
    _, secret = store.create_display_token("studio monitor")
    assert store.verify_display_token(secret)
    # The whole point: it cannot reach inference.
    assert not store.verify_api_key(secret)


def test_an_api_key_does_not_open_the_display(store: CredentialStore) -> None:
    _, secret = store.create_key("laptop")
    assert store.verify_api_key(secret)
    assert not store.verify_display_token(secret)


def test_tokens_are_distinguishable_on_sight(store: CredentialStore) -> None:
    _, display = store.create_display_token("wall")
    _, api = store.create_key("laptop")
    assert display.startswith("engd-")
    assert api.startswith("eng-") and not api.startswith("engd-")


def test_only_the_hash_is_persisted(store: CredentialStore) -> None:
    _, secret = store.create_display_token("wall")
    assert secret not in store.path.read_text(encoding="utf-8")


def test_the_last_display_token_may_be_revoked(store: CredentialStore) -> None:
    """Unlike an API key, removing the last one locks nobody out.

    It only turns the wall view off; an admin session still opens it.
    """
    token, secret = store.create_display_token("wall")
    store.revoke_display_token(token.id)
    assert not store.verify_display_token(secret)
    assert store.list_display_tokens() == []


def test_the_last_api_key_still_cannot_be_revoked(store: CredentialStore) -> None:
    keys = store.list_keys()
    assert len(keys) == 1
    with pytest.raises(CredentialError):
        store.revoke_key(keys[0].id)


def test_revoking_an_unknown_token_is_an_error(store: CredentialStore) -> None:
    with pytest.raises(KeyError):
        store.revoke_display_token("not-a-real-id")


def test_a_store_written_before_display_tokens_existed_still_works(
    tmp_path: Path,
) -> None:
    """The field is created on demand.

    An existing install must not need its credential file hand-edited before a
    display can be paired.
    """
    store = CredentialStore(
        tmp_path / "credentials.json",
        seed_api_keys="seed-key-0000000000",
        seed_admin_token="a-long-enough-password",
    )
    data = store._data
    assert data is not None
    del data["display_tokens"]

    assert store.list_display_tokens() == []
    _, secret = store.create_display_token("wall")
    assert store.verify_display_token(secret)
