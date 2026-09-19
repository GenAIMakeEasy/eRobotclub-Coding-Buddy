"""AdminAuth is deliberately minimal (see its module docstring): one
password, one bearer token per login, in-memory only. These tests cover the
security-relevant edges — no password configured, wrong password, expiry."""

import time

from app.admin.auth import AdminAuth


def test_login_succeeds_with_the_right_password():
    auth = AdminAuth(password="secret")
    token = auth.login("secret")
    assert token is not None
    assert auth.check(token)


def test_login_fails_with_the_wrong_password():
    auth = AdminAuth(password="secret")
    assert auth.login("wrong") is None


def test_login_is_disabled_when_no_password_is_configured():
    auth = AdminAuth(password=None)
    assert auth.enabled is False
    assert auth.login("anything") is None
    assert auth.login("") is None


def test_check_rejects_an_unknown_token():
    auth = AdminAuth(password="secret")
    assert auth.check("made-up-token") is False


def test_check_rejects_none():
    auth = AdminAuth(password="secret")
    assert auth.check(None) is False


def test_tokens_expire_after_their_ttl():
    auth = AdminAuth(password="secret", token_ttl_seconds=0.05)
    token = auth.login("secret")
    assert auth.check(token)
    time.sleep(0.1)
    assert auth.check(token) is False


def test_each_login_issues_a_distinct_token():
    auth = AdminAuth(password="secret")
    t1 = auth.login("secret")
    t2 = auth.login("secret")
    assert t1 != t2
    assert auth.check(t1)
    assert auth.check(t2)


def test_a_callable_password_source_is_re_evaluated_on_every_login():
    # This is the whole point of accepting a callable, not just a string:
    # the real app wires in config.current_admin_password, which re-reads
    # .env live -- caught live, a hand-edited ADMIN_PASSWORD kept failing
    # with "wrong password" because AdminAuth held the OLD value frozen
    # from process start. A mutable box standing in for "the file changed
    # under us" is the simplest way to prove the source is read fresh
    # each call, not cached at construction.
    current = {"value": "old-password"}
    auth = AdminAuth(password=lambda: current["value"])

    assert auth.login("old-password") is not None
    assert auth.login("new-password") is None  # not yet changed

    current["value"] = "new-password"
    assert auth.login("old-password") is None  # old value no longer works
    token = auth.login("new-password")
    assert token is not None
    assert auth.check(token)


def test_a_callable_password_source_can_disable_login_dynamically():
    current = {"value": None}
    auth = AdminAuth(password=lambda: current["value"])
    assert auth.enabled is False
    assert auth.login("anything") is None

    current["value"] = "secret"
    assert auth.enabled is True
    assert auth.login("secret") is not None
