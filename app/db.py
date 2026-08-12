import os
import sqlite3
from contextlib import contextmanager
from typing import Generator

DATA_DIR = os.getenv("DATA_DIR", "./data")
DB_PATH = os.path.join(DATA_DIR, "app.db")


def get_db_path() -> str:
    os.makedirs(DATA_DIR, exist_ok=True)
    return DB_PATH


@contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    path = get_db_path()
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                youtube_channel_id TEXT NOT NULL,
                display_name TEXT NOT NULL,
                stream_key TEXT NOT NULL,
                video_count INTEGER NOT NULL DEFAULT 20,
                check_interval_hours INTEGER NOT NULL DEFAULT 6,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'stopped',
                last_checked_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS channel_videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id TEXT NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
                youtube_video_id TEXT NOT NULL,
                file_path TEXT,
                downloaded_at TIMESTAMP,
                position INTEGER,
                status TEXT NOT NULL DEFAULT 'ready',
                error_message TEXT,
                UNIQUE(channel_id, youtube_video_id)
            );
        """)
        
        # Migration: Add status and error_message columns if they don't exist
        try:
            conn.execute("ALTER TABLE channel_videos ADD COLUMN status TEXT NOT NULL DEFAULT 'ready'")
        except sqlite3.OperationalError:
            pass # Column already exists
            
        try:
            conn.execute("ALTER TABLE channel_videos ADD COLUMN error_message TEXT")
        except sqlite3.OperationalError:
            pass # Column already exists
