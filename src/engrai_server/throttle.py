"""Failure throttling for the login endpoint.

Deliberately in-process and unbounded-in-time rather than a full rate limiter:
this gateway serves one person on a LAN, and the only thing worth slowing down
is repeated password guessing from a single source.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class LoginThrottle:
    max_failures: int = 5
    lockout_seconds: int = 300
    _failures: dict[str, list[float]] = field(default_factory=dict)

    def blocked_for(self, client: str) -> int:
        """Seconds the client must wait, or 0 if it may attempt a login."""
        attempts = self._recent(client)
        if len(attempts) < self.max_failures:
            return 0
        unblocks_at = attempts[-1] + self.lockout_seconds
        return max(0, int(unblocks_at - time.time()))

    def record_failure(self, client: str) -> None:
        self._failures.setdefault(client, []).append(time.time())

    def reset(self, client: str) -> None:
        self._failures.pop(client, None)

    def _recent(self, client: str) -> list[float]:
        cutoff = time.time() - self.lockout_seconds
        attempts = [stamp for stamp in self._failures.get(client, []) if stamp > cutoff]
        if attempts:
            self._failures[client] = attempts
        else:
            self._failures.pop(client, None)
        return attempts
