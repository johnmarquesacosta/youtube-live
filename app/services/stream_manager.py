import os
import time
import logging
import threading
import subprocess
from typing import Dict, Optional
from app.db import get_db
from app.services.playlist import generate_playlist, get_playlist_path

logger = logging.getLogger(__name__)


class StreamManager:
    def __init__(self):
        self._processes: Dict[str, subprocess.Popen] = {}
        self._monitor_threads: Dict[str, threading.Thread] = {}
        self._stop_events: Dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def is_running(self, channel_id: str) -> bool:
        with self._lock:
            proc = self._processes.get(channel_id)
            if proc is not None and proc.poll() is None:
                return True
            return False

    def start_stream(self, channel_id: str) -> bool:
        with self._lock:
            # Check if channel details exist in DB
            with get_db() as conn:
                channel = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
                if not channel:
                    logger.error(f"Channel {channel_id} not found in database.")
                    return False
                stream_key = channel["stream_key"]

            # Ensure valid playlist.txt
            playlist_path = get_playlist_path(channel_id)
            if not os.path.exists(playlist_path) or os.path.getsize(playlist_path) == 0:
                playlist_path = generate_playlist(channel_id)

            if not os.path.exists(playlist_path) or os.path.getsize(playlist_path) == 0:
                logger.error(f"Cannot start stream for {channel_id}: playlist is empty or missing.")
                with get_db() as conn:
                    conn.execute("UPDATE channels SET status = 'error' WHERE id = ?", (channel_id,))
                return False

            # Terminate existing process if any
            if channel_id in self._processes:
                self._stop_process_unlocked(channel_id)

            # Build FFmpeg RTMP command
            rtmp_url = f"rtmp://a.rtmp.youtube.com/live2/{stream_key}"
            cmd = [
                "ffmpeg",
                "-re",
                "-stream_loop", "-1",
                "-f", "concat",
                "-safe", "0",
                "-i", playlist_path,
                "-c:v", "copy",
                "-c:a", "copy",
                "-f", "flv",
                rtmp_url
            ]

            logger.info(f"Starting FFmpeg stream for channel {channel_id}...")
            try:
                # Redirect output to prevent pipe buffer deadlock
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL
                )
                self._processes[channel_id] = proc
                self._stop_events[channel_id] = threading.Event()

                # Start monitor thread
                stop_evt = self._stop_events[channel_id]
                monitor = threading.Thread(
                    target=self._monitor_process,
                    args=(channel_id, proc, stop_evt),
                    daemon=True
                )
                self._monitor_threads[channel_id] = monitor
                monitor.start()

                with get_db() as conn:
                    conn.execute("UPDATE channels SET status = 'running', is_active = 1 WHERE id = ?", (channel_id,))

                return True
            except Exception as e:
                logger.error(f"Failed to spawn FFmpeg process for {channel_id}: {e}")
                with get_db() as conn:
                    conn.execute("UPDATE channels SET status = 'error' WHERE id = ?", (channel_id,))
                return False

    def stop_stream(self, channel_id: str) -> bool:
        with self._lock:
            self._stop_process_unlocked(channel_id)
            with get_db() as conn:
                conn.execute("UPDATE channels SET status = 'stopped', is_active = 0 WHERE id = ?", (channel_id,))
        return True

    def restart_stream(self, channel_id: str) -> bool:
        """Restarts FFmpeg stream keeping is_active setting unchanged."""
        logger.info(f"Restarting stream for channel {channel_id}...")
        with self._lock:
            self._stop_process_unlocked(channel_id)

        # Regenerate playlist
        generate_playlist(channel_id)

        with get_db() as conn:
            channel = conn.execute("SELECT is_active FROM channels WHERE id = ?", (channel_id,)).fetchone()
            if channel and channel["is_active"]:
                return self.start_stream(channel_id)
        return False

    def _stop_process_unlocked(self, channel_id: str):
        if channel_id in self._stop_events:
            self._stop_events[channel_id].set()

        proc = self._processes.pop(channel_id, None)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            except Exception as e:
                logger.warning(f"Error terminating process for {channel_id}: {e}")

        self._monitor_threads.pop(channel_id, None)
        self._stop_events.pop(channel_id, None)

    def _monitor_process(self, channel_id: str, proc: subprocess.Popen, stop_event: threading.Event):
        """Background thread monitoring FFmpeg subprocess status."""
        proc.wait()
        if stop_event.is_set():
            # Intentional stop
            return

        exit_code = proc.returncode
        logger.warning(f"FFmpeg process for channel {channel_id} exited unexpectedly with code {exit_code}")

        # Check if channel is still supposed to be active
        with get_db() as conn:
            channel = conn.execute("SELECT is_active FROM channels WHERE id = ?", (channel_id,)).fetchone()
            is_active = channel["is_active"] if channel else False

        if is_active:
            logger.info(f"Auto-restarting stream for active channel {channel_id} in 5 seconds...")
            time.sleep(5)
            if not stop_event.is_set():
                self.start_stream(channel_id)
        else:
            with get_db() as conn:
                conn.execute("UPDATE channels SET status = 'stopped' WHERE id = ?", (channel_id,))


# Global StreamManager singleton
stream_manager = StreamManager()
