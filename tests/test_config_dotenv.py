"""write_dotenv_updates persists admin settings changes back to .env so they
survive a restart. Line-based, not a full parser (see its docstring) —
these tests cover the round-trip: update existing keys in place, append new
ones, leave everything else (comments, unrelated keys) untouched."""

from app.config import _compute_code_fingerprint, current_admin_password, write_dotenv_updates


def test_updates_an_existing_key_in_place(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_BACKEND=local\nOTHER_KEY=untouched\n", encoding="utf-8")

    write_dotenv_updates({"MODEL_BACKEND": "gemini"}, path=env_path)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "MODEL_BACKEND=gemini" in lines
    assert "OTHER_KEY=untouched" in lines


def test_appends_a_new_key_that_did_not_exist(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_BACKEND=local\n", encoding="utf-8")

    write_dotenv_updates({"GEMINI_API_KEY": "sk-test-123"}, path=env_path)

    lines = env_path.read_text(encoding="utf-8").splitlines()
    assert "GEMINI_API_KEY=sk-test-123" in lines
    assert "MODEL_BACKEND=local" in lines


def test_creates_the_file_when_it_does_not_exist_yet(tmp_path):
    env_path = tmp_path / ".env"
    assert not env_path.exists()

    write_dotenv_updates({"MODEL_BACKEND": "gemini"}, path=env_path)

    assert env_path.exists()
    assert "MODEL_BACKEND=gemini" in env_path.read_text(encoding="utf-8")


def test_comments_and_blank_lines_survive_untouched(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("# a comment\nMODEL_BACKEND=local\n\n", encoding="utf-8")

    write_dotenv_updates({"MODEL_BACKEND": "gemini"}, path=env_path)

    text = env_path.read_text(encoding="utf-8")
    assert "# a comment" in text
    assert "MODEL_BACKEND=gemini" in text


def test_the_current_processs_environ_is_updated_too(tmp_path, monkeypatch):
    import os

    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_BACKEND=local\n", encoding="utf-8")
    monkeypatch.delenv("MODEL_BACKEND_TEST_PROBE", raising=False)

    write_dotenv_updates({"MODEL_BACKEND_TEST_PROBE": "value"}, path=env_path)

    assert os.environ["MODEL_BACKEND_TEST_PROBE"] == "value"
    del os.environ["MODEL_BACKEND_TEST_PROBE"]


def test_current_admin_password_reflects_a_hand_edit_with_no_restart(tmp_path):
    # This is the whole reason current_admin_password exists rather than
    # just reading config.ADMIN_PASSWORD: that constant is frozen at
    # process start (os.environ.setdefault in _load_dotenv never
    # overwrites an already-set var), which is exactly what made a fresh
    # .env password edit keep failing with "wrong password" until a
    # restart, caught live against the real running server.
    env_path = tmp_path / ".env"
    env_path.write_text("ADMIN_PASSWORD=first\n", encoding="utf-8")
    assert current_admin_password(path=env_path) == "first"

    env_path.write_text("ADMIN_PASSWORD=second\n", encoding="utf-8")
    assert current_admin_password(path=env_path) == "second"


def test_current_admin_password_falls_back_when_env_file_omits_it(tmp_path, monkeypatch):
    # The original deployment shape this setting was designed for: a real
    # env var, no .env file (or a .env that doesn't mention it) at all.
    env_path = tmp_path / ".env"
    env_path.write_text("MODEL_BACKEND=local\n", encoding="utf-8")
    monkeypatch.setattr("app.config.ADMIN_PASSWORD", "fallback-value")

    assert current_admin_password(path=env_path) == "fallback-value"


def test_current_admin_password_handles_a_missing_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.ADMIN_PASSWORD", "fallback-value")
    assert current_admin_password(path=tmp_path / "does-not-exist.env") == "fallback-value"


def test_code_fingerprint_changes_when_a_source_file_changes(tmp_path):
    # The whole point of exposing this via /health: it must actually
    # reflect the current content of app/, not just "a fingerprint was
    # computed once and never checked again".
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    before = _compute_code_fingerprint(app_dir)

    (app_dir / "a.py").write_text("x = 2\n", encoding="utf-8")
    after = _compute_code_fingerprint(app_dir)

    assert before != after
    assert _compute_code_fingerprint(app_dir) == after  # deterministic, not random


def test_code_fingerprint_ignores_file_order(tmp_path):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "a.py").write_text("a\n", encoding="utf-8")
    (app_dir / "b.py").write_text("b\n", encoding="utf-8")
    first = _compute_code_fingerprint(app_dir)

    # Rewriting in a different order (deleting and recreating b before a)
    # must not change the result -- files are walked in a sorted, stable
    # order, not filesystem/creation order.
    (app_dir / "b.py").unlink()
    (app_dir / "b.py").write_text("b\n", encoding="utf-8")
    assert _compute_code_fingerprint(app_dir) == first
