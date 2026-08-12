import os
import glob
import logging
import subprocess
from datetime import datetime
from typing import List, Dict, Any
from app.db import get_db
from app.services.playlist import get_referenced_files
from app.services.job_queue import enqueue_download

logger = logging.getLogger(__name__)

DATA_DIR = os.getenv("DATA_DIR", "./data")
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "1800"))   # 30 min
TRANSCODE_TIMEOUT = int(os.getenv("TRANSCODE_TIMEOUT", "3600"))  # 1 hour


def get_channel_videos_dir(channel_id: str) -> str:
    videos_dir = os.path.join(DATA_DIR, channel_id, "videos")
    os.makedirs(videos_dir, exist_ok=True)
    return videos_dir


def get_cookies_path() -> str:
    """Returns path to cookies.txt if it exists in DATA_DIR."""
    cookie_file = os.path.join(DATA_DIR, "cookies.txt")
    if os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 0:
        return cookie_file
    return ""


def _check_is_live(video_id: str, cookies_path: str) -> bool:
    """
    Quick pre-download check: queries yt-dlp to determine if a video is
    currently live or upcoming. Returns True if the video should be skipped.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--print", "%(is_live)s|%(live_status)s",
        "--no-warnings",
    ]
    if cookies_path:
        cmd.extend(["--cookies", cookies_path])
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            # If we can't determine status, err on the side of caution and allow download
            return False
        output = result.stdout.strip()
        if not output:
            return False
        parts = output.split("|")
        is_live_str = parts[0].strip().lower() if len(parts) > 0 else ""
        live_status = parts[1].strip().lower() if len(parts) > 1 else ""

        if is_live_str == "true" or live_status in ("is_live", "is_upcoming"):
            logger.info(f"Video {video_id} is live/upcoming (is_live={is_live_str}, live_status={live_status}). Skipping.")
            return True
        return False
    except subprocess.TimeoutExpired:
        logger.warning(f"Live-check timed out for {video_id}. Allowing download attempt.")
        return False
    except Exception as e:
        logger.warning(f"Live-check failed for {video_id}: {e}. Allowing download attempt.")
        return False


def download_and_normalize_video(channel_id: str, video_id: str) -> str:
    """
    Downloads raw video with yt-dlp, reencodes to standardized MP4
    (H.264 + AAC, 1920x1080 30fps) for seamless FFmpeg concat demuxer playback,
    and returns the normalized file path.
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

    # Cookies are REQUIRED for datacenter IPs (Coolify / VPS)
    if not cookies_path:
        raise RuntimeError(
            f"cookies.txt ausente em {DATA_DIR}/cookies.txt — obrigatório em IP de datacenter. "
            "Upload cookies via Settings > Cookies no dashboard."
        )

    # Pre-download live check — prevent downloading own live stream
    if _check_is_live(video_id, cookies_path):
        raise LiveVideoError(f"Video {video_id} is live/upcoming — skipping download")

    dl_cmd = [
        "yt-dlp",
        "-f", "bv*[height<=1080]+ba/b",
        "--extractor-args", "youtube:player_client=web,mweb,android",
        "--no-playlist",
        "--sleep-requests", "1",
        "--retries", "5",
        "--retry-sleep", "10",
        "--cookies", cookies_path,
        "-o", raw_template,
        url,
    ]

    try:
        result = subprocess.run(dl_cmd, capture_output=True, text=True, timeout=DOWNLOAD_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"yt-dlp download timed out for {video_id} after {DOWNLOAD_TIMEOUT}s"
        )

    if result.returncode != 0:
        error_msg = result.stderr or result.stdout
        raise RuntimeError(
            f"yt-dlp failed for {video_id}: {error_msg[:500]}"
        )

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

    try:
        subprocess.run(transcode_cmd, check=True, timeout=TRANSCODE_TIMEOUT)
    except subprocess.TimeoutExpired:
        # Clean up partial output
        if os.path.exists(normalized_path):
            os.remove(normalized_path)
        raise RuntimeError(
            f"FFmpeg transcode timed out for {video_id} after {TRANSCODE_TIMEOUT}s"
        )

    # Clean up raw file
    if os.path.exists(raw_path) and raw_path != normalized_path:
        try:
            os.remove(raw_path)
        except Exception as e:
            logger.warning(f"Could not remove raw file {raw_path}: {e}")

    return normalized_path


class LiveVideoError(Exception):
    """Raised when a video is detected as live/upcoming and should be skipped."""
    pass


def sync_channel_videos(channel_id: str, target_video_ids: List[str]) -> bool:
    """
    Downloads missing videos in target_video_ids and prunes older videos.
    Downloads go through the global serial queue (job_queue) to prevent
    parallel downloads that trigger bot detection.
    Returns True if the list of downloaded videos changed, False otherwise.
    """
    changed = False

    with get_db() as conn:
        existing_rows = conn.execute(
            "SELECT youtube_video_id, file_path, status FROM channel_videos WHERE channel_id = ?",
            (channel_id,)
        ).fetchall()
        existing_map = {row["youtube_video_id"]: row for row in existing_rows}

    # 1. Insert any new target videos as 'pending'
    for idx, video_id in enumerate(target_video_ids):
        if video_id not in existing_map:
            with get_db() as conn:
                conn.execute("""
                    INSERT INTO channel_videos (channel_id, youtube_video_id, status, position)
                    VALUES (?, ?, 'pending', ?)
                """, (channel_id, video_id, idx))
            changed = True
            # Update the map so we can process it in the next step
            existing_map[video_id] = {"file_path": None, "status": "pending"}

    # 2. Process downloads for videos that are not 'ready'
    #    Downloads are enqueued into the global serial queue.
    futures = []
    for idx, video_id in enumerate(target_video_ids):
        video_record = existing_map.get(video_id)
        
        # We only need to download if it's not ready or if the file doesn't exist
        is_ready = video_record and video_record["status"] == "ready"
        file_path = video_record["file_path"] if video_record else None
        file_exists = file_path and os.path.exists(file_path)
        
        if not is_ready or not file_exists:
            # Enqueue download through the global serial worker
            future = enqueue_download(
                channel_id, video_id,
                download_and_normalize_video,
                channel_id, video_id
            )
            futures.append((idx, video_id, future))
        else:
            # Update position if needed
            with get_db() as conn:
                conn.execute(
                    "UPDATE channel_videos SET position = ? WHERE channel_id = ? AND youtube_video_id = ?",
                    (idx, channel_id, video_id)
                )

    # Wait for all enqueued downloads to complete
    for idx, video_id, future in futures:
        try:
            norm_path = future.result()  # blocks until this download finishes
            now_str = datetime.utcnow().isoformat()
            with get_db() as conn:
                conn.execute("""
                    UPDATE channel_videos SET
                        file_path = ?,
                        downloaded_at = ?,
                        position = ?,
                        status = 'ready',
                        error_message = NULL
                    WHERE channel_id = ? AND youtube_video_id = ?
                """, (norm_path, now_str, idx, channel_id, video_id))
            changed = True
        except LiveVideoError as e:
            logger.info(f"Skipped live/upcoming video {video_id} for channel {channel_id}: {e}")
            with get_db() as conn:
                conn.execute("""
                    UPDATE channel_videos SET
                        status = 'skipped_live',
                        error_message = ?,
                        position = ?
                    WHERE channel_id = ? AND youtube_video_id = ?
                """, (str(e), idx, channel_id, video_id))
            changed = True
        except Exception as e:
            logger.error(f"Error downloading/normalizing video {video_id} for channel {channel_id}: {e}")
            error_msg = str(e)
            with get_db() as conn:
                conn.execute("""
                    UPDATE channel_videos SET
                        status = 'error',
                        error_message = ?,
                        position = ?
                    WHERE channel_id = ? AND youtube_video_id = ?
                """, (error_msg, idx, channel_id, video_id))
            changed = True

    # 3. Prune old videos no longer in target_video_ids
    #    Respects referenced files — won't delete files still in active/pending playlists
    target_set = set(target_video_ids)
    referenced_files = get_referenced_files(channel_id)

    for v_id, record in existing_map.items():
        if v_id not in target_set:
            file_path = record["file_path"]
            if file_path and os.path.exists(file_path):
                # Check if file is still referenced in active or pending playlist
                abs_file_path = os.path.abspath(file_path).replace("\\", "/")
                if abs_file_path in referenced_files:
                    logger.info(
                        f"Skipping prune of {v_id} — file still referenced in active/pending playlist. "
                        "Will be pruned in next cycle after playlist swap."
                    )
                    continue

                logger.info(f"Pruning video {v_id} for channel {channel_id}...")
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
