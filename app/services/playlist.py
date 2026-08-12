import os
import re
import logging
from app.db import get_db

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")


def get_playlist_path(channel_id: str) -> str:
    channel_dir = os.path.join(DATA_DIR, channel_id)
    os.makedirs(channel_dir, exist_ok=True)
    return os.path.join(channel_dir, "playlist.txt")


def get_pending_playlist_path(channel_id: str) -> str:
    """Returns path to the pending playlist (playlist_new.txt)."""
    channel_dir = os.path.join(DATA_DIR, channel_id)
    os.makedirs(channel_dir, exist_ok=True)
    return os.path.join(channel_dir, "playlist_new.txt")


def generate_playlist(channel_id: str) -> str:
    """
    Generates playlist_new.txt (pending) for FFmpeg concat demuxer based on
    active channel_videos. The pending playlist is only promoted to
    playlist.txt when apply_pending_playlist() is called (with ffmpeg stopped).
    Returns absolute path to the pending playlist file.
    """
    pending_path = get_pending_playlist_path(channel_id)

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
        # Write an empty pending file so apply_pending_playlist can handle it
        if os.path.exists(pending_path):
            os.remove(pending_path)
        return pending_path

    # Write pending playlist (playlist_new.txt)
    with open(pending_path, "w", encoding="utf-8") as f:
        for path in valid_paths:
            f.write(f"file '{path}'\n")

    logger.info(f"Generated pending playlist for {channel_id} with {len(valid_paths)} videos at {pending_path}")
    return pending_path


def apply_pending_playlist(channel_id: str) -> bool:
    """
    Promotes playlist_new.txt to playlist.txt atomically.
    Must be called ONLY when FFmpeg is stopped for this channel.
    Returns True if a pending playlist was applied, False otherwise.
    """
    pending_path = get_pending_playlist_path(channel_id)
    active_path = get_playlist_path(channel_id)

    if not os.path.exists(pending_path):
        return False

    try:
        os.replace(pending_path, active_path)
        logger.info(f"Applied pending playlist for {channel_id}: {pending_path} -> {active_path}")
        return True
    except Exception as e:
        logger.error(f"Failed to apply pending playlist for {channel_id}: {e}")
        return False


def get_referenced_files(channel_id: str) -> set:
    """
    Reads both playlist.txt (active) and playlist_new.txt (pending) and
    returns the set of all file paths referenced in either.
    This is used by the prune step to avoid deleting files that are still
    needed by the running or upcoming playlist.
    """
    referenced = set()
    paths_to_check = [
        get_playlist_path(channel_id),
        get_pending_playlist_path(channel_id),
    ]

    # Pattern to extract file path from FFmpeg concat format: file '/path/to/video.mp4'
    file_pattern = re.compile(r"^file\s+'(.+)'\s*$")

    for playlist_path in paths_to_check:
        if not os.path.exists(playlist_path):
            continue
        try:
            with open(playlist_path, "r", encoding="utf-8") as f:
                for line in f:
                    match = file_pattern.match(line.strip())
                    if match:
                        # Un-escape single quotes from FFmpeg concat syntax
                        file_path = match.group(1).replace("'\\''", "'")
                        referenced.add(file_path)
        except Exception as e:
            logger.warning(f"Error reading playlist {playlist_path}: {e}")

    return referenced
