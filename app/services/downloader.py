import os
import glob
import logging
import subprocess
from datetime import datetime
from typing import List, Dict, Any
from app.db import get_db

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")


def get_channel_videos_dir(channel_id: str) -> str:
    videos_dir = os.path.join(DATA_DIR, channel_id, "videos")
    os.makedirs(videos_dir, exist_ok=True)
    return videos_dir


def get_cookies_path() -> str:
    """Check for cookies file in DATA_DIR or create one from YOUTUBE_COOKIES env."""
    cookie_file = os.path.join(DATA_DIR, "cookies.txt")
    if os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 0:
        return cookie_file
    
    cookies_env = os.getenv("YOUTUBE_COOKIES", "").strip()
    if cookies_env:
        with open(cookie_file, "w", encoding="utf-8") as f:
            f.write(cookies_env)
        return cookie_file
        
    return ""


def download_and_normalize_video(channel_id: str, video_id: str) -> str:
    """
    Downloads raw video with yt-dlp using anti-bot player clients & filters,
    reencodes to standardized MP4 (H.264 + AAC, 1920x1080 30fps),
    and returns normalized file path.
    """
    videos_dir = get_channel_videos_dir(channel_id)
    raw_template = os.path.join(videos_dir, f"{video_id}_raw.%(ext)s")
    normalized_path = os.path.join(videos_dir, f"{video_id}.mp4")

    # If already normalized and exists, return path
    if os.path.exists(normalized_path) and os.path.getsize(normalized_path) > 0:
        return normalized_path

    url = f"https://www.youtube.com/watch?v={video_id}"
    logger.info(f"Downloading video {video_id} for channel {channel_id}...")

    cookies_path = get_cookies_path()

    # Try different player client strategies to bypass YouTube datacenter bot checks
    client_strategies = [
        "youtube:player_client=android,ios,web",
        "youtube:player_client=ios,web",
        "youtube:player_client=mweb,android",
        "youtube:player_client=tv,web"
    ]

    download_success = False
    last_error = None

    for client_args in client_strategies:
        dl_cmd = [
            "yt-dlp",
            "-f", "bv*[height<=1080]+ba/b",
            "--extractor-args", client_args,
            "--no-playlist",
            "--match-filter", "!is_live & !upcoming & live_status != 'is_live' & live_status != 'is_incoming'",
            "-o", raw_template,
        ]

        if cookies_path:
            dl_cmd.extend(["--cookies", cookies_path])

        dl_cmd.append(url)

        try:
            res = subprocess.run(dl_cmd, capture_output=True, text=True, check=True)
            download_success = True
            break
        except subprocess.CalledProcessError as e:
            last_error = e.stderr or e.stdout
            logger.warning(f"yt-dlp attempt with '{client_args}' for {video_id} failed: {last_error[:200] if last_error else str(e)}")

    if not download_success:
        raise RuntimeError(f"All yt-dlp download strategies failed for {video_id}: {last_error}")

    # Find raw downloaded file
    raw_files = glob.glob(os.path.join(videos_dir, f"{video_id}_raw.*"))
    if not raw_files:
        raise FileNotFoundError(f"Raw downloaded file for {video_id} not found after yt-dlp run.")

    raw_path = raw_files[0]
    logger.info(f"Normalizing video {raw_path} to {normalized_path}...")

    # Transcode to H.264 + AAC, 1920x1080 30fps with letterboxing if needed
    transcode_cmd = [
        "ffmpeg", "-y",
        "-i", raw_path,
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,fps=30",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ar", "44100",
        normalized_path
    ]
    subprocess.run(transcode_cmd, check=True)

    # Clean up raw file
    if os.path.exists(raw_path) and raw_path != normalized_path:
        try:
            os.remove(raw_path)
        except Exception as e:
            logger.warning(f"Could not remove raw file {raw_path}: {e}")

    return normalized_path


def sync_channel_videos(channel_id: str, target_video_ids: List[str]) -> bool:
    """
    Downloads missing videos in target_video_ids and prunes older videos.
    Returns True if the list of downloaded videos changed, False otherwise.
    """
    changed = False

    with get_db() as conn:
        existing_rows = conn.execute(
            "SELECT youtube_video_id, file_path FROM channel_videos WHERE channel_id = ?",
            (channel_id,)
        ).fetchall()
        existing_map = {row["youtube_video_id"]: row["file_path"] for row in existing_rows}

    # 1. Download missing videos
    for idx, video_id in enumerate(target_video_ids):
        existing_file = existing_map.get(video_id)
        if not existing_file or not os.path.exists(existing_file):
            try:
                norm_path = download_and_normalize_video(channel_id, video_id)
                now_str = datetime.utcnow().isoformat()
                with get_db() as conn:
                    conn.execute("""
                        INSERT INTO channel_videos (channel_id, youtube_video_id, file_path, downloaded_at, position)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(channel_id, youtube_video_id) DO UPDATE SET
                            file_path = excluded.file_path,
                            downloaded_at = excluded.downloaded_at,
                            position = excluded.position
                    """, (channel_id, video_id, norm_path, now_str, idx))
                changed = True
            except Exception as e:
                logger.error(f"Error downloading/normalizing video {video_id} for channel {channel_id}: {e}")
        else:
            # Update position if needed
            with get_db() as conn:
                conn.execute(
                    "UPDATE channel_videos SET position = ? WHERE channel_id = ? AND youtube_video_id = ?",
                    (idx, channel_id, video_id)
                )

    # 2. Prune old videos no longer in target_video_ids
    target_set = set(target_video_ids)
    for v_id, file_path in existing_map.items():
        if v_id not in target_set:
            logger.info(f"Pruning video {v_id} for channel {channel_id}...")
            if file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except Exception as e:
                    logger.warning(f"Error deleting video file {file_path}: {e}")
            with get_db() as conn:
                conn.execute(
                    "DELETE FROM channel_videos WHERE channel_id = ? AND youtube_video_id = ?",
                    (channel_id, v_id)
                )
            changed = True

    return changed
