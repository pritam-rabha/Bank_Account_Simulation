import os

import pytest

os.environ.setdefault("SECRET_KEY", "test-secret-key")

from app import create_app

BASE_CONFIG = {
    "TESTING": True,
    "SECRET_KEY": "test-secret-key",
    "SEED_SAMPLE_DATA": False,
    "SESSION_COOKIE_SECURE": False,
}


def build_app(tmp_path, **overrides):
    config = {**BASE_CONFIG, "DATA_FILE": str(tmp_path / "data" / "bank.json"), **overrides}
    return create_app(config)


@pytest.fixture
def app(tmp_path):
    """A fresh application whose JSON file lives in an isolated temp directory."""
    return build_app(tmp_path)


@pytest.fixture
def bank(app):
    return app.extensions["bank"]


@pytest.fixture
def client(app):
    return app.test_client()
