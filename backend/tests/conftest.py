"""Put the backend package on the path and share the API client."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app, limiter

    limiter.reset()
    with TestClient(app) as test_client:
        yield test_client
    limiter.reset()
