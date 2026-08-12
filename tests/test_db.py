import os
import tempfile
import pytest
from app import db


def test_init_db(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(db, "DATA_DIR", tmpdir)
        monkeypatch.setattr(db, "DB_PATH", os.path.join(tmpdir, "app.db"))

        db.init_db()
        assert os.path.exists(os.path.join(tmpdir, "app.db"))

        with db.get_db() as conn:
            channels = conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
            tables = [c["name"] for c in channels]
            assert "channels" in tables
            assert "channel_videos" in tables
