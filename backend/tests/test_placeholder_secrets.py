"""Unedited .env.example placeholders must never become real secrets."""

import pytest

from app.config import Settings


@pytest.mark.parametrize("val", ["SET_ME", "GENERATE_AT_DEPLOY", "set_by_entrypoint",
                                 "  ChangeMe ", ""])
def test_placeholder_values_are_treated_as_unset(monkeypatch, val):
    for name in ("UI_PASSWORD", "SESSION_SECRET", "TOTP_ENCRYPTION_KEY"):
        monkeypatch.setenv(name, val)
    s = Settings()
    assert s.ui_password == "" and s.session_secret == "" and s.totp_key == ""


def test_real_values_pass_through(monkeypatch):
    monkeypatch.setenv("SESSION_SECRET", "a" * 64)
    monkeypatch.setenv("UI_PASSWORD", "correct horse battery")
    s = Settings()
    assert s.session_secret == "a" * 64 and s.ui_password == "correct horse battery"
