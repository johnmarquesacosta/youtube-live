import os
import time
import queue
import logging
import threading
from concurrent.futures import Future
from typing import Any, Tuple

logger = logging.getLogger(__name__)

DOWNLOAD_PAUSE = int(os.getenv("DOWNLOAD_PAUSE", "20"))

# Global serial download queue — ensures only one download runs at a time
# across ALL channels, preventing bot detection from parallel requests.
_job_q: queue.Queue[Tuple[Any, ...] | None] = queue.Queue()
_worker_thread: threading.Thread | None = None
_shutdown_event = threading.Event()


def enqueue_download(channel_id: str, video_id: str, download_fn, *args, **kwargs) -> Future:
    """
    Enqueue a download job to be processed serially by the global worker.
    
    Args:
        channel_id: Channel identifier for logging
        video_id: Video identifier for logging  
        download_fn: The callable to execute (e.g. download_and_normalize_video)
        *args, **kwargs: Arguments to pass to download_fn
    
    Returns:
        A Future that will contain the result (or exception) of the download.
    """
    future = Future()
    _job_q.put((future, channel_id, video_id, download_fn, args, kwargs))
    logger.info(f"Enqueued download for video {video_id} (channel {channel_id}). Queue size: ~{_job_q.qsize()}")
    return future


def _worker():
    """
    Serial worker thread that processes one download at a time with
    DOWNLOAD_PAUSE seconds between jobs. This mirrors the ancapsutv
    pattern of serialized downloads to avoid bot detection.
    """
    logger.info(f"Download worker started (DOWNLOAD_PAUSE={DOWNLOAD_PAUSE}s)")
    last_job_time = 0.0

    while not _shutdown_event.is_set():
        try:
            item = _job_q.get(timeout=1.0)
        except queue.Empty:
            continue

        if item is None:
            # Poison pill — shutdown signal
            logger.info("Download worker received shutdown signal.")
            break

        future, channel_id, video_id, download_fn, args, kwargs = item

        # If the future was already cancelled, skip
        if future.cancelled():
            logger.info(f"Skipping cancelled download for {video_id} (channel {channel_id})")
            _job_q.task_done()
            continue

        # Enforce pause between downloads
        elapsed = time.monotonic() - last_job_time
        if elapsed < DOWNLOAD_PAUSE and last_job_time > 0:
            wait_time = DOWNLOAD_PAUSE - elapsed
            logger.info(f"Waiting {wait_time:.1f}s before next download (rate-limiting)...")
            # Wait in small increments so we can respond to shutdown
            deadline = time.monotonic() + wait_time
            while time.monotonic() < deadline and not _shutdown_event.is_set():
                time.sleep(min(1.0, deadline - time.monotonic()))
            if _shutdown_event.is_set():
                future.cancel()
                _job_q.task_done()
                break

        logger.info(f"Processing download: video {video_id} for channel {channel_id}")
        try:
            result = download_fn(*args, **kwargs)
            future.set_result(result)
        except Exception as e:
            future.set_exception(e)
            logger.error(f"Download failed for video {video_id} (channel {channel_id}): {e}")

        last_job_time = time.monotonic()
        _job_q.task_done()

    logger.info("Download worker stopped.")


def start_download_worker():
    """Start the global serial download worker thread."""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        logger.warning("Download worker is already running.")
        return

    _shutdown_event.clear()
    _worker_thread = threading.Thread(target=_worker, daemon=True, name="download-worker")
    _worker_thread.start()
    logger.info("Download worker thread started.")


def stop_download_worker():
    """Stop the global serial download worker thread gracefully."""
    global _worker_thread
    _shutdown_event.set()
    # Send poison pill to unblock the queue.get()
    _job_q.put(None)
    if _worker_thread is not None:
        _worker_thread.join(timeout=10)
        if _worker_thread.is_alive():
            logger.warning("Download worker did not stop within timeout.")
        _worker_thread = None
    logger.info("Download worker stopped.")
