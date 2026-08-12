import os
import logging
from app.db import get_db

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")


def get_playlist_path(channel_id: str) -> str:
    channel_dir = os.path.join(DATA_DIR, channel_id)
    os.makedirs(channel_dir, exist_ok=True)
    return os.path.join(channel_dir, "playlist.txt")


def generate_playlist(channel_id: str) -> str:
    """
    Generates playlist.txt for FFmpeg concat demuxer based on active channel_videos.
    Returns absolute path to playlist.txt.
    """
    playlist_path = get_playlist_path(channel_id)

    with get_db() as conn:
        rows = conn.execute("""
            SELECT file_path FROM channel_videos
            WHERE channel_id = ? AND file_path IS NOT NULL
            ORDER BY position ASC, downloaded_at DESC
        """, (channel_id,)).fetchall()

    valid_paths = []
    for row in rows:
        fp = row["file_path"]
        if fp and os.path.exists(fp) and os.path.getsize(fp) > 0:
            # Ensure absolute path with normalized forward slashes for FFmpeg concat file format
            abs_fp = os.path.abspath(fp).replace("\\", "/")
            # Escape single quotes for FFmpeg concat syntax
            escaped_fp = abs_fp.replace("'", "'\\''")
            valid_paths.append(escaped_fp)

    if not valid_paths:
        logger.warning(f"No valid video files found to build playlist for channel {channel_id}")
        if os.path.exists(playlist_path):
            os.remove(playlist_path)
        return playlist_path

    # Write playlist.txt
    with open(playlist_path, "w", encoding="utf-8") as f:
        for path in valid_paths:
            f.write(f"file '{path}'\n")

    logger.info(f"Generated playlist for {channel_id} with {len(valid_paths)} videos at {playlist_path}")
    return playlist_path
