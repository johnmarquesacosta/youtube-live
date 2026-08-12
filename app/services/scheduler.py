import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.db import get_db
from app.services.fetcher import fetch_latest_video_ids
from app.services.downloader import sync_channel_videos
from app.services.playlist import generate_playlist
from app.services.stream_manager import stream_manager

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


def sync_channel_job(channel_id: str):
    """
    Executes the sync pipeline for a channel:
    1. Fetch latest video IDs
    2. Download new videos & prune old ones
    3. Update last_checked_at
    4. If videos changed, regenerate playlist & restart stream if active
    """
    logger.info(f"Running scheduled sync job for channel {channel_id}...")
    with get_db() as conn:
        channel = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not channel:
            logger.warning(f"Channel {channel_id} not found during sync job execution.")
            return

    youtube_channel_id = channel["youtube_channel_id"]
    video_count = channel["video_count"]
    is_active = bool(channel["is_active"])

    try:
        latest_ids = fetch_latest_video_ids(youtube_channel_id, video_count)
        changed = sync_channel_videos(channel_id, latest_ids)
        now_str = datetime.utcnow().isoformat()

        with get_db() as conn:
            conn.execute("UPDATE channels SET last_checked_at = ? WHERE id = ?", (now_str, channel_id))

        if changed:
            logger.info(f"Video list changed for {channel_id}. Rebuilding playlist...")
            generate_playlist(channel_id)
            if is_active and stream_manager.is_running(channel_id):
                stream_manager.restart_stream(channel_id)
    except Exception as e:
        logger.error(f"Error executing sync job for channel {channel_id}: {e}")


def check_all_channels_job():
    """
    Global scheduled check job.
    Iterates over all channels and checks if check_interval_hours has elapsed since last_checked_at.
    """
    logger.info("Checking all channels for scheduled updates...")
    with get_db() as conn:
        channels = conn.execute("SELECT * FROM channels").fetchall()

    now = datetime.utcnow()
    for channel in channels:
        channel_id = channel["id"]
        interval = channel["check_interval_hours"]
        last_checked = channel["last_checked_at"]

        should_check = False
        if not last_checked:
            should_check = True
        else:
            try:
                dt = datetime.fromisoformat(last_checked)
                hours_diff = (now - dt).total_seconds() / 3600.0
                if hours_diff >= interval:
                    should_check = True
            except Exception:
                should_check = True

        if should_check:
            sync_channel_job(channel_id)


def start_scheduler():
    if not scheduler.running:
        # Run check_all_channels_job every 15 minutes to evaluate check_interval_hours for each channel
        scheduler.add_job(check_all_channels_job, 'interval', minutes=15, id='check_all_channels', replace_existing=True)
        scheduler.start()
        logger.info("APScheduler started successfully.")


def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler stopped.")
