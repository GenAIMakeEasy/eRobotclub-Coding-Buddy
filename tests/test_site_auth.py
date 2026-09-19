"""SiteAuth: stateless signed-cookie gate for the whole student-facing app
(distinct from AdminAuth, which gates the separate admin panel). Same
security-relevant edges as test_admin_auth.py -- no password configured,
wrong password, expiry -- plus the properties specific to being stateless
(survives across instances, invalidated by a password change)."""

import time

from app.site_auth import SiteAuth


def test_a_correct_password_issues_a_cookie_that_then_checks_out():
    auth = SiteAuth(password="robotclub")
    cookie = auth.issue_cookie("robotclub")
    assert cookie is not None
    assert auth.check_cookie(cookie)


def test_a_wrong_password_issues_no_cookie():
    auth = SiteAuth(password="robotclub")
    assert auth.issue_cookie("wrong") is None


def test_no_password_configured_disables_the_gate_entirely():
    # Same convention as AdminAuth: unset means the feature is simply off,
    # not "impossible to log in" -- here that means every request passes.
    auth = SiteAuth(password=None)
    assert auth.enabled is False
    assert auth.check_cookie(None) is True
    assert auth.check_cookie("garbage") is True


def test_a_missing_or_garbage_cookie_is_rejected_when_a_password_is_set():
    auth = SiteAuth(password="robotclub")
    assert auth.check_cookie(None) is False
    assert auth.check_cookie("") is False
    assert auth.check_cookie("not-a-real-cookie") is False


def test_an_expired_cookie_is_rejected():
    auth = SiteAuth(password="robotclub", ttl_seconds=0.05)
    cookie = auth.issue_cookie("robotclub")
    assert auth.check_cookie(cookie)
    time.sleep(0.1)
    assert auth.check_cookie(cookie) is False


def test_a_cookie_is_stateless_and_verifiable_by_a_fresh_instance():
    # The whole point of NOT using AdminAuth's in-memory token dict: a
    # server restart (frequent during active development on the Mac mini)
    # must not force every student to re-enter the password.
    issuer = SiteAuth(password="robotclub")
    cookie = issuer.issue_cookie("robotclub")
    fresh_instance = SiteAuth(password="robotclub")
    assert fresh_instance.check_cookie(cookie)


def test_changing_the_password_invalidates_every_existing_cookie():
    current = {"value": "robotclub"}
    auth = SiteAuth(password=lambda: current["value"])
    cookie = auth.issue_cookie("robotclub")
    assert auth.check_cookie(cookie)

    current["value"] = "newpassword"
    assert auth.check_cookie(cookie) is False


def test_a_callable_password_source_is_re_evaluated_live():
    current = {"value": None}
    auth = SiteAuth(password=lambda: current["value"])
    assert auth.enabled is False

    current["value"] = "robotclub"
    assert auth.enabled is True
    cookie = auth.issue_cookie("robotclub")
    assert auth.check_cookie(cookie)
