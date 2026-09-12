import pytest


@pytest.fixture(autouse=True)
def mock_aws_env(monkeypatch):
    # This runs before tests and ensures the environment is set early
    monkeypatch.setenv("AWS_DEFAULT_ACCOUNT", "000000000000")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "mock_key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "mock_secret")
