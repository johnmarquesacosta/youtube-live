import os
import json
import logging
import subprocess
from typing import List
import httpx

logger = logging.getLogger(__name__)

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")


def get_channel_uploads_playlist_id(channel_identifier: str, api_key: str = "") -> str:
    """Fetch the uploads playlist ID using YouTube Data API v3."""
    key = api_key or YOUTUBE_API_KEY
    if not key:
        raise ValueError("YOUTUBE_API_KEY is not configured")

    channel_identifier = channel_identifier.strip()
    url = "https://www.googleapis.com/youtube/v3/channels"
    
    params = {
        "part": "contentDetails",
        "key": key
    }
    
    if channel_identifier.startswith("@"):
        params["forHandle"] = channel_identifier
    elif channel_identifier.startswith("UC"):
        params["id"] = channel_identifier
    else:
        # Try as ID first, fallback handled by caller or API
        params["id"] = channel_identifier

    with httpx.Client(timeout=10.0) as client:
        res = client.get(url, params=params)
        res.raise_for_status()
        data = res.json()
        
        items = data.get("items", [])
        if not items and not channel_identifier.startswith("UC") and not channel_identifier.startswith("@"):
            # Try forUsername if ID query yielded no results
            params.pop("id", None)
            params["forUsername"] = channel_identifier
            res = client.get(url, params=params)
            res.raise_for_status()
            data = res.json()
            items = data.get("items", [])

        if not items:
            raise ValueError(f"Channel not found for identifier: {channel_identifier}")

        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        return uploads_id


def fetch_latest_video_ids_api(channel_identifier: str, video_count: int = 20, api_key: str = "") -> List[str]:
    """Fetch recent video IDs from channel using YouTube Data API v3."""
    key = api_key or YOUTUBE_API_KEY
    uploads_playlist_id = get_channel_uploads_playlist_id(channel_identifier, key)
    
    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {
        "part": "snippet",
        "playlistId": uploads_playlist_id,
        "maxResults": min(video_count, 50),
        "key": key
    }
    
    with httpx.Client(timeout=10.0) as client:
        res = client.get(url, params=params)
        res.raise_for_status()
        data = res.json()
        
        video_ids = []
        for item in data.get("items", []):
            resource = item.get("snippet", {}).get("resourceId", {})
            if resource.get("kind") == "youtube#video":
                video_ids.append(resource.get("videoId"))
        return video_ids[:video_count]


def fetch_latest_video_ids_ytdlp(channel_identifier: str, video_count: int = 20) -> List[str]:
    """Fallback: Fetch recent video IDs using yt-dlp flat playlist extraction."""
    channel_identifier = channel_identifier.strip()
    if channel_identifier.startswith("@") or channel_identifier.startswith("UC"):
        channel_url = f"https://www.youtube.com/{channel_identifier}/videos"
    else:
        channel_url = f"https://www.youtube.com/channel/{channel_identifier}/videos"

    cmd = [
        "yt-dlp",
        "--flat-playlist",
        "--playlist-end", str(video_count * 2),
        "--extractor-args", "youtube:player_client=android,ios,web",
        "--dump-json",
        channel_url
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        video_ids = []
        for line in res.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
                v_id = data.get("id")
                if v_id:
                    video_ids.append(v_id)
            except json.JSONDecodeError:
                continue
        return video_ids[:video_count]
    except Exception as e:
        logger.error(f"yt-dlp fetch failed for {channel_identifier}: {e}")
        raise


def fetch_latest_video_ids(channel_identifier: str, video_count: int = 20) -> List[str]:
    """
    Primary fetcher: tries YouTube Data API v3 if API key is provided,
    falls back to yt-dlp extraction if API fails or API key is not set.
    """
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    if key:
        try:
            return fetch_latest_video_ids_api(channel_identifier, video_count, key)
        except Exception as e:
            logger.warning(f"YouTube Data API fetch failed for {channel_identifier}: {e}. Falling back to yt-dlp...")
    
    return fetch_latest_video_ids_ytdlp(channel_identifier, video_count)
