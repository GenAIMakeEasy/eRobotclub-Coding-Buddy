"""A single shared password gating the whole student-facing app -- distinct
from AdminAuth (app/admin/auth.py), which gates the separate admin panel.
This one exists because the app is about to be reachable from the public
internet (a Cloudflare Tunnel), not just the school's own wifi, and a
class's worth of strangers finding the URL should not be able to burn
through the owner's OpenRouter/Gemini credits or poke at the admin login.

Deliberately a STATELESS signed cookie, not an in-memory token dict like
AdminAuth's -- this needs to survive a server restart (the Mac mini's
LaunchAgent/manual restarts happen often during active development) without
kicking every student back to a password prompt, and there's no need to be
able to revoke one specific session early the way an admin token can be.
Changing SITE_PASSWORD invalidates every existing cookie automatically,
since the signature is computed over the CURRENT password -- no separate
revocation mechanism needed.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Callable, Optional, Union

PasswordSource = Union[str, None, Callable[[], "str | None"]]

COOKIE_NAME = "site_access"
_DEFAULT_TTL_SECONDS = 90 * 24 * 3600  # 90 days -- long enough a student never re-enters it mid-term


class SiteAuth:
    def __init__(self, password: PasswordSource, ttl_seconds: float = _DEFAULT_TTL_SECONDS):
        self._password_source = password
        self.ttl_seconds = ttl_seconds

    @property
    def password(self) -> Optional[str]:
        """Same live-source pattern as AdminAuth.password -- a callable is
        re-evaluated on every access so a hand-edited .env takes effect
        immediately, not just at process start."""
        return self._password_source() if callable(self._password_source) else self._password_source

    @property
    def enabled(self) -> bool:
        return bool(self.password)

    def _sign(self, expires_at: float) -> str:
        key = (self.password or "").encode("utf-8")
        # Fixed precision, not the raw float repr -- str(float) can vary
        # (trailing zeros, scientific notation for tiny/huge values) in a
        # way that would make a value the signer computed with one
        # representation fail to verify against a slightly different one.
        return hmac.new(key, f"{expires_at:.3f}".encode("utf-8"), hashlib.sha256).hexdigest()

    def issue_cookie(self, password: str) -> Optional[str]:
        """Returns a new cookie value on a correct password, None on wrong
        (or no password configured -- login is simply disabled then, same
        convention as AdminAuth.login when ADMIN_PASSWORD is unset)."""
        if not self.enabled or not hmac.compare_digest(password, self.password):
            return None
        expires_at = time.time() + self.ttl_seconds
        # ":" as the separator, not "." -- the expiry itself is formatted
        # WITH a decimal point ("1234.567"), so "." would collide and
        # split in the wrong place.
        return f"{expires_at:.3f}:{self._sign(expires_at)}"

    def check_cookie(self, cookie_value: Optional[str]) -> bool:
        if not self.enabled:
            # No SITE_PASSWORD configured at all -- gate is simply off,
            # same "unconfigured means disabled" convention as AdminAuth.
            return True
        if not cookie_value or ":" not in cookie_value:
            return False
        expires_str, _, signature = cookie_value.partition(":")
        try:
            expires_at = float(expires_str)
        except ValueError:
            return False
        if time.time() > expires_at:
            return False
        return hmac.compare_digest(signature, self._sign(expires_at))
