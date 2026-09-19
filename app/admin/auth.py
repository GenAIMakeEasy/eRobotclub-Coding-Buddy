"""Admin authentication: one password (config.ADMIN_PASSWORD), one bearer
token per login, held in memory. This is a LAN-only tool for one teacher-
admin, not a multi-user system — deliberately no user accounts, no password
hashing library, no database. A token surviving only until server restart
(plus a TTL) is an acceptable tradeoff at this scale; it is not acceptable
for anything internet-facing.
"""

from __future__ import annotations

import secrets
import time
from typing import Callable, Union

# Either a fixed value (what every existing test passes, and still the
# right shape for anything that doesn't live-reload) or a zero-arg callable
# re-evaluated on every access -- see AdminAuth.password below for why the
# real app wires in the latter.
PasswordSource = Union[str, None, Callable[[], "str | None"]]


class AdminAuth:
    def __init__(self, password: PasswordSource, token_ttl_seconds: float = 8 * 3600):
        self._password_source = password
        self.token_ttl_seconds = token_ttl_seconds
        self._tokens: dict[str, float] = {}  # token -> expires_at

    @property
    def password(self) -> str | None:
        """The password AS OF RIGHT NOW. A plain string/None is returned
        unchanged (the fixed-value case every test uses); a callable is
        invoked fresh on every access instead of once at construction --
        the real app wires in `config.current_admin_password`, which
        re-reads .env's ADMIN_PASSWORD line live, so a hand-edit to that
        file takes effect on the very next login attempt instead of being
        frozen until the server restarts (caught live: this was exactly
        the gap that made a fresh .env password edit keep failing with
        "wrong password")."""
        return self._password_source() if callable(self._password_source) else self._password_source

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def login(self, password: str) -> str | None:
        """Returns a new token on success, None on a wrong password or when
        no ADMIN_PASSWORD is configured at all (login is simply disabled)."""
        current = self.password  # one live read, not two (enabled + compare)
        if not current or not secrets.compare_digest(password, current):
            return None
        token = secrets.token_urlsafe(32)
        self._tokens[token] = time.monotonic() + self.token_ttl_seconds
        return token

    def check(self, token: str | None) -> bool:
        if not token:
            return False
        expires_at = self._tokens.get(token)
        if expires_at is None:
            return False
        if time.monotonic() > expires_at:
            del self._tokens[token]
            return False
        return True
