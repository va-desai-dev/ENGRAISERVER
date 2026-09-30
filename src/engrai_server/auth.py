from __future__ import annotations

import secrets
from collections.abc import Iterable

from fastapi import Header, HTTPException, status

from .credentials import CredentialStore


def extract_bearer(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, separator, token = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return token.strip()
    return ""


def token_matches(candidate: str, allowed: Iterable[str]) -> bool:
    return bool(candidate) and any(
        secrets.compare_digest(candidate, expected) for expected in allowed
    )


def build_dependencies(store: CredentialStore):
    """Build the auth dependencies bound to one credential store.

    The store is mutable at runtime, so the dependencies close over the
    instance rather than re-reading settings on each call.
    """

    def require_api_key(authorization: str | None = Header(default=None)) -> str:
        token = extract_bearer(authorization)
        if not store.verify_api_key(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "error": {
                        "message": "Invalid API key",
                        "type": "invalid_request_error",
                        "code": "invalid_api_key",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token

    def require_display(authorization: str | None = Header(default=None)) -> str:
        """Read-only access to the wall view.

        A display token is accepted, and so is an admin session — otherwise an
        operator could not preview the display without first pairing one. The
        reverse does not hold: this is the only dependency a display token
        satisfies, so it cannot reach /v1 or any /control write.
        """
        token = extract_bearer(authorization)
        if store.verify_display_token(token):
            return token
        if store.verify_session(token):
            return token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid display token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    def require_admin(authorization: str | None = Header(default=None)) -> str:
        """Accept a session token, or the admin password directly.

        The session path is what the browser uses and is a cheap dict lookup.
        The password path costs a scrypt verification, and exists so curl and
        scripts can hit /control without logging in first.
        """
        token = extract_bearer(authorization)
        if store.verify_session(token):
            return token
        # Do not spend a password-KDF operation on an empty Authorization
        # header. Besides being wasted work, it lets anonymous request floods
        # consume the expensive scrypt lane.
        if token and store.verify_admin_password(token):
            return token
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid control-plane credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return require_api_key, require_admin, require_display
