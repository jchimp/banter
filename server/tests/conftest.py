import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, api_key="test-key", _env_file=None)


@pytest.fixture
def client(settings) -> TestClient:
    # TestClient as a context manager runs lifespan (and therefore migrations).
    with TestClient(create_app(settings)) as c:
        yield c
