import os
import tempfile
import pytest
from app import db
from app.services import playlist


def test_generate_playlist(monkeypatch):
    with tempfile.TemporaryDirectory() as tmpdir:
        monkeypatch.setattr(db, "DATA_DIR", tmpdir)
        monkeypatch.setattr(db, "DB_PATH", os.path.join(tmpdir, "app.db"))
        monkeypatch.setattr(playlist, "DATA_DIR", tmpdir)

        db.init_db()

        # Create dummy video file
        videos_dir = os.path.join(tmpdir, "test-channel", "videos")
        os.makedirs(videos_dir, exist_ok=True)
        dummy_video = os.path.join(videos_dir, "vid1.mp4")
        with open(dummy_video, "w") as f:
            f.write("dummy video data")

        # Insert channel and channel_video record
        with db.get_db() as conn:
            conn.execute("""
                INSERT INTO channels (id, youtube_channel_id, display_name, stream_key)
                VALUES ('test-channel', 'UC123', 'Test Channel', 'key123')
            """)
            conn.execute("""
                INSERT INTO channel_videos (channel_id, youtube_video_id, file_path, position)
                VALUES ('test-channel', 'vid1', ?, 0)
            """, (dummy_video,))

        playlist_file = playlist.generate_playlist("test-channel")
        assert os.path.exists(playlist_file)

        with open(playlist_file, "r") as f:
            content = f.read()
            assert "file '" in content
            assert "vid1.mp4'" in content
