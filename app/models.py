from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field, field_validator
import re


class ChannelBase(BaseModel):
    youtube_channel_id: str = Field(..., description="YouTube Channel ID (e.g. UCxxxxxxxx) or handle")
    display_name: str = Field(..., min_length=1, max_length=100)
    stream_key: str = Field(..., min_length=1)
    video_count: int = Field(20, ge=1, le=100)
    check_interval_hours: int = Field(6, ge=1, le=168)
    is_active: bool = True

    @field_validator("youtube_channel_id")
    @classmethod
    def clean_channel_id(cls, v: str) -> str:
        return v.strip()


class ChannelCreate(ChannelBase):
    id: str = Field(..., min_length=1, max_length=50, description="Internal slug ID")

    @field_validator("id")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        v = v.strip().lower()
        if not re.match(r"^[a-z0-9\-_]+$", v):
            raise ValueError("ID must contain only lowercase letters, numbers, hyphens, and underscores.")
        return v


class ChannelUpdate(BaseModel):
    youtube_channel_id: str
    display_name: str
    stream_key: str
    video_count: int = Field(20, ge=1, le=100)
    check_interval_hours: int = Field(6, ge=1, le=168)
    is_active: bool = True


class Channel(ChannelBase):
    id: str
    status: str = "stopped"
    last_checked_at: Optional[str] = None
    created_at: Optional[str] = None


class ChannelVideo(BaseModel):
    id: Optional[int] = None
    channel_id: str
    youtube_video_id: str
    file_path: Optional[str] = None
    downloaded_at: Optional[str] = None
    position: Optional[int] = None


class DashboardStats(BaseModel):
    total_channels: int = 0
    active_streams: int = 0
    running_streams: int = 0
    total_videos: int = 0
    storage_bytes: int = 0
    storage_formatted: str = "0 B"
