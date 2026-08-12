import pytest
from app.auth import verify_credentials, APP_USERNAME, APP_PASSWORD


def test_verify_credentials():
    assert verify_credentials(APP_USERNAME, APP_PASSWORD) is True
    assert verify_credentials("wrong_user", APP_PASSWORD) is False
    assert verify_credentials(APP_USERNAME, "wrong_pass") is False
    assert verify_credentials("", "") is False
