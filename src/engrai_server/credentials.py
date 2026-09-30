"""Mutable API keys and admin password, owned by the app rather than the env.

Env vars seed this store on first run and are ignored afterwards, so keys can
be minted and the password changed from the web UI without editing a file and
restarting the unit.

Two different hashes are used on purpose:

* API keys are 256-bit random tokens this app generates, so a fast SHA-256 is
  correct — there is no dictionary to attack, and the check sits on the /v1
  request path where a slow KDF would be felt.
* The admin password is human-chosen and therefore needs scrypt. Because that
  is far too slow to run on every polled /control request, a successful login
  mints a random session token which subsequent requests present instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# scrypt parameters. n=2**15 keeps a single verification near 60ms on this
# hardware, which is a sensible login cost and irrelevant elsewhere given the
# session token.
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SESSION_TTL_SECONDS = 12 * 3600
KEY_PREFIX = "eng"
# A distinct prefix so a display token is never mistaken for an inference key
# in a log, a paste, or a support question. They authorise different things:
# an API key can run generation, a display token can only read a status page.
DISPLAY_PREFIX = "engd"
MIN_PASSWORD_LENGTH = 12


class CredentialError(ValueError):
    pass


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _maxmem(n: int, r: int, p: int) -> int:
    """Memory budget to hand OpenSSL for these parameters.

    scrypt needs 128*n*r bytes, which at n=2**15, r=8 is exactly the 32 MiB
    that OpenSSL allows by default — so the default rejects its own working
    set. Ask for headroom rather than tuning the cost down.
    """
    return 2 * 128 * n * r * p + (1 << 20)


def hash_password(password: str) -> dict[str, Any]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        maxmem=_maxmem(SCRYPT_N, SCRYPT_R, SCRYPT_P),
    )
    return {
        "algorithm": "scrypt",
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
        "salt": salt.hex(),
        "hash": digest.hex(),
    }


def verify_password(password: str, record: dict[str, Any]) -> bool:
    try:
        n, r, p = int(record["n"]), int(record["r"]), int(record["p"])
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(record["salt"]),
            n=n,
            r=r,
            p=p,
            maxmem=_maxmem(n, r, p),
        )
    except (KeyError, ValueError, TypeError):
        return False
    return secrets.compare_digest(digest.hex(), str(record.get("hash", "")))


def generate_api_key() -> str:
    return f"{KEY_PREFIX}-{secrets.token_urlsafe(32)}"


def generate_display_token() -> str:
    return f"{DISPLAY_PREFIX}-{secrets.token_urlsafe(32)}"


def derive_initials(name: str, email: str = "") -> str:
    """Two-letter monogram for the avatar, falling back the way names do.

    A person with one name gets its first two letters rather than a lonely
    single character, and an account created with only an email still gets
    something recognisable instead of a placeholder.
    """
    words = [word for word in re.split(r"[^0-9A-Za-z]+", name) if word]
    if len(words) >= 2:
        return (words[0][0] + words[-1][0]).upper()
    if words:
        return words[0][:2].upper()
    local = email.split("@", 1)[0]
    letters = [character for character in local if character.isalnum()]
    return "".join(letters[:2]).upper() or "EN"


@dataclass(frozen=True, slots=True)
class ApiKey:
    id: str
    label: str
    prefix: str
    created_at: float
    last_used_at: float | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "prefix": self.prefix,
            "created_at": self.created_at,
            "last_used_at": self.last_used_at,
        }


class CredentialStore:
    def __init__(self, path: Path, *, seed_api_keys: str = "", seed_admin_token: str = ""):
        self.path = path
        self._sessions: dict[str, float] = {}
        self._data = self._load()
        if self._data is None and seed_admin_token.strip():
            # Environment seeding stays supported for existing deployments and
            # for scripted installs.
            self._data = self._bootstrap(seed_api_keys, seed_admin_token)
            self._write()

    @property
    def initialized(self) -> bool:
        """False on a first run with no store and no environment seed.

        The app serves an onboarding screen in that state rather than refusing
        to start, so a fresh clone is set up in a browser instead of a text
        editor.
        """
        return self._data is not None

    def _require(self) -> dict[str, Any]:
        if self._data is None:
            raise CredentialError("Credential store is not initialised")
        return self._data

    def initialize(self, *, name: str, email: str, password: str) -> str:
        """Create the first account. Returns the first API key in plaintext."""
        if self._data is not None:
            # Guards against a second setup request racing the first, which
            # would otherwise reset the password of a live install.
            raise CredentialError("Already initialised")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise CredentialError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if not name.strip():
            raise CredentialError("Name is required")
        secret = generate_api_key()
        now = time.time()
        self._data = {
            "version": 1,
            "profile": {
                "name": name.strip(),
                "email": email.strip(),
                "created_at": now,
            },
            "admin": hash_password(password),
            "api_keys": [
                {
                    "id": secrets.token_hex(8),
                    "label": "Created during setup",
                    "prefix": secret[:12],
                    "sha256": _sha256(secret),
                    "created_at": now,
                    "last_used_at": None,
                }
            ],
        }
        self._write()
        return secret

    # --------------------------------------------------------------- profile

    def profile(self) -> dict[str, Any]:
        stored = (self._data or {}).get("profile") or {}
        name = str(stored.get("name") or "")
        email = str(stored.get("email") or "")
        return {
            "name": name,
            "email": email,
            "initials": derive_initials(name, email),
        }

    def update_profile(self, *, name: str, email: str) -> dict[str, Any]:
        data = self._require()
        if not name.strip():
            raise CredentialError("Name is required")
        data.setdefault("profile", {})
        data["profile"]["name"] = name.strip()
        data["profile"]["email"] = email.strip()
        self._write()
        return self.profile()

    # ---------------------------------------------------------------- storage

    def _load(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or "admin" not in payload:
            return None
        payload.setdefault("api_keys", [])
        return payload

    def _bootstrap(self, seed_api_keys: str, seed_admin_token: str) -> dict[str, Any]:
        keys = [item.strip() for item in seed_api_keys.split(",") if item.strip()]
        if not keys:
            raise CredentialError("ENGRAI_API_KEYS must contain at least one key")
        now = time.time()
        return {
            "version": 1,
            "profile": {"name": "Administrator", "email": "", "created_at": now},
            "admin": hash_password(seed_admin_token.strip()),
            "display_tokens": [],
            "api_keys": [
                {
                    "id": secrets.token_hex(8),
                    "label": "Seeded from environment",
                    "prefix": key[:12],
                    "sha256": _sha256(key),
                    "created_at": now,
                    "last_used_at": None,
                }
                for key in keys
            ],
        }

    def _write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        # Create with restrictive permissions before any secret material is
        # written, rather than chmod-ing a file that briefly existed as 0644.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(self._data, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)

    # -------------------------------------------------------- display tokens

    def _display_records(self) -> list[dict[str, Any]]:
        """The display-token list, created on demand.

        A credential file written before display tokens existed has no such
        key, and a wall display must not require the operator to hand-edit
        JSON to start working.
        """
        data = self._require()
        records = data.get("display_tokens")
        if not isinstance(records, list):
            records = []
            data["display_tokens"] = records
        return records

    def list_display_tokens(self) -> list[ApiKey]:
        if self._data is None:
            return []
        return [
            ApiKey(
                id=str(item["id"]),
                label=str(item.get("label") or ""),
                prefix=str(item.get("prefix") or ""),
                created_at=float(item.get("created_at") or 0.0),
                last_used_at=item.get("last_used_at"),
            )
            for item in self._display_records()
        ]

    def verify_display_token(self, candidate: str) -> bool:
        """Constant-time check against the stored hashes.

        Deliberately separate from verify_api_key: an inference key must not
        open the display, and a display token must never reach /v1.
        """
        if not candidate or self._data is None:
            return False
        digest = _sha256(candidate)
        for item in self._display_records():
            if secrets.compare_digest(digest, str(item.get("sha256", ""))):
                # In memory only, like an API key: a wall display polls
                # continuously and must not cause a disk write per poll.
                item["last_used_at"] = time.time()
                return True
        return False

    def create_display_token(self, label: str) -> tuple[ApiKey, str]:
        secret = generate_display_token()
        self._display_records().append(
            {
                "id": secrets.token_hex(8),
                "label": label.strip() or "Wall display",
                "prefix": secret[:13],
                "sha256": _sha256(secret),
                "created_at": time.time(),
                "last_used_at": None,
            }
        )
        self._write()
        return self.list_display_tokens()[-1], secret

    def revoke_display_token(self, token_id: str) -> None:
        """Unlike an API key, the last one may be revoked.

        Removing every display token only turns the wall view off; it cannot
        lock anybody out, because the admin session opens it too.
        """
        records = self._display_records()
        remaining = [item for item in records if item["id"] != token_id]
        if len(remaining) == len(records):
            raise KeyError(token_id)
        self._require()["display_tokens"] = remaining
        self._write()

    # ------------------------------------------------------------- api  keys

    def list_keys(self) -> list[ApiKey]:
        if self._data is None:
            return []
        return [
            ApiKey(
                id=str(item["id"]),
                label=str(item.get("label") or ""),
                prefix=str(item.get("prefix") or ""),
                created_at=float(item.get("created_at") or 0.0),
                last_used_at=item.get("last_used_at"),
            )
            for item in self._data["api_keys"]
        ]

    def verify_api_key(self, candidate: str) -> bool:
        if not candidate or self._data is None:
            return False
        digest = _sha256(candidate)
        for item in self._data["api_keys"]:
            if secrets.compare_digest(digest, str(item.get("sha256", ""))):
                # Recorded in memory only. Persisting on the request path would
                # mean a disk write per inference call for a cosmetic field;
                # it is flushed whenever the store is next written.
                item["last_used_at"] = time.time()
                return True
        return False

    def create_key(self, label: str) -> tuple[ApiKey, str]:
        secret = generate_api_key()
        record = {
            "id": secrets.token_hex(8),
            "label": label.strip() or "Unnamed key",
            "prefix": secret[:12],
            "sha256": _sha256(secret),
            "created_at": time.time(),
            "last_used_at": None,
        }
        self._require()["api_keys"].append(record)
        self._write()
        keys = self.list_keys()
        return keys[-1], secret

    def revoke_key(self, key_id: str) -> None:
        data = self._require()
        remaining = [item for item in data["api_keys"] if item["id"] != key_id]
        if len(remaining) == len(data["api_keys"]):
            raise KeyError(key_id)
        if not remaining:
            # Every OpenAI client authenticates with these. Emptying the list
            # from the UI would lock out the API with no way back in through
            # the API itself.
            raise CredentialError(
                "Cannot revoke the last API key — create a replacement first"
            )
        data["api_keys"] = remaining
        self._write()

    # -------------------------------------------------------------- password

    def verify_admin_password(self, password: str) -> bool:
        if not password or self._data is None:
            return False
        return verify_password(password, self._data["admin"])

    def change_password(self, current: str, replacement: str) -> None:
        if not self.verify_admin_password(current):
            raise CredentialError("Current password is incorrect")
        if len(replacement) < MIN_PASSWORD_LENGTH:
            raise CredentialError(
                f"New password must be at least {MIN_PASSWORD_LENGTH} characters"
            )
        if replacement == current:
            raise CredentialError("New password must differ from the current one")
        self._require()["admin"] = hash_password(replacement)
        self._write()
        # Every existing session was authorised by the old password.
        self._sessions.clear()

    # -------------------------------------------------------------- sessions

    def create_session(self) -> tuple[str, float]:
        self._prune_sessions()
        token = secrets.token_urlsafe(32)
        expires_at = time.time() + SESSION_TTL_SECONDS
        self._sessions[token] = expires_at
        return token, expires_at

    def verify_session(self, token: str) -> bool:
        if not token:
            return False
        expires_at = self._sessions.get(token)
        if expires_at is None:
            return False
        if expires_at < time.time():
            self._sessions.pop(token, None)
            return False
        return True

    def revoke_session(self, token: str) -> None:
        self._sessions.pop(token, None)

    def _prune_sessions(self) -> None:
        now = time.time()
        for token in [token for token, exp in self._sessions.items() if exp < now]:
            del self._sessions[token]
