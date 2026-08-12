import os
import tempfile
import pytest
from fastapi.testclient import TestClient
from app import db
from app.main import app


@pytest.fixture
def client(monkeypatch):
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr(db, "DATA_DIR", tmpdir)
    monkeypatch.setattr(db, "DB_PATH", os.path.join(tmpdir, "app.db"))
    db.init_db()

    with TestClient(app) as test_client:
        yield test_client


def test_login_flow(client):
    # Unauthenticated access redirects to /login
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/login"

    # Login page renders
    res = client.get("/login")
    assert res.status_code == 200
    assert "Sign In" in res.text

    # Invalid credentials
    res = client.post("/login", data={"username": "admin", "password": "wrongpassword"})
    assert res.status_code == 401

    # Valid credentials
    res = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"] == "/"
