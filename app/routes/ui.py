import os
import shutil
import logging
from typing import Optional
from fastapi import APIRouter, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.auth import verify_credentials, is_authenticated
from app.db import get_db
from app.models import ChannelCreate, ChannelUpdate, DashboardStats
from app.services.stream_manager import stream_manager
from app.services.scheduler import sync_channel_job

logger = logging.getLogger(__name__)

templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    return templates.TemplateResponse(request=request, name="login.html", context={"hide_nav": True})


@router.post("/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if verify_credentials(username, password):
        request.session["authenticated"] = True
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"error": "Invalid username or password.", "hide_nav": True},
        status_code=status.HTTP_401_UNAUTHORIZED
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with get_db() as conn:
        channels_rows = conn.execute("SELECT * FROM channels ORDER BY created_at DESC").fetchall()
        channels = [dict(r) for r in channels_rows]
        
        # Calculate dashboard stats
        total_channels = len(channels)
        active_streams = sum(1 for c in channels if c["is_active"])
        running_streams = sum(1 for c in channels if c["status"] == "running")
        
        video_count_row = conn.execute("SELECT COUNT(*) as cnt FROM channel_videos WHERE file_path IS NOT NULL").fetchone()
        total_videos = video_count_row["cnt"] if video_count_row else 0

    stats = DashboardStats(
        total_channels=total_channels,
        active_streams=active_streams,
        running_streams=running_streams,
        total_videos=total_videos
    )

    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "channels": channels,
        "stats": stats
    })


@router.get("/channels/new", response_class=HTMLResponse)
async def new_channel_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    return templates.TemplateResponse(request=request, name="channel_form.html", context={
        "is_edit": False
    })


@router.post("/channels/new", response_class=HTMLResponse)
async def create_channel(
    request: Request,
    id: str = Form(...),
    youtube_channel_id: str = Form(...),
    display_name: str = Form(...),
    stream_key: str = Form(...),
    video_count: int = Form(20),
    check_interval_hours: int = Form(6),
    is_active: Optional[str] = Form(None)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    is_active_bool = is_active is not None

    try:
        channel_data = ChannelCreate(
            id=id,
            youtube_channel_id=youtube_channel_id,
            display_name=display_name,
            stream_key=stream_key,
            video_count=video_count,
            check_interval_hours=check_interval_hours,
            is_active=is_active_bool
        )
    except Exception as e:
        return templates.TemplateResponse(request=request, name="channel_form.html", context={
            "is_edit": False,
            "error": str(e),
            "form_data": {
                "id": id,
                "youtube_channel_id": youtube_channel_id,
                "display_name": display_name,
                "stream_key": stream_key,
                "video_count": video_count,
                "check_interval_hours": check_interval_hours
            }
        }, status_code=status.HTTP_400_BAD_REQUEST)

    with get_db() as conn:
        existing = conn.execute("SELECT id FROM channels WHERE id = ?", (channel_data.id,)).fetchone()
        if existing:
            return templates.TemplateResponse(request=request, name="channel_form.html", context={
                "is_edit": False,
                "error": f"Channel with ID '{channel_data.id}' already exists.",
                "form_data": channel_data.model_dump()
            }, status_code=status.HTTP_400_BAD_REQUEST)

        conn.execute("""
            INSERT INTO channels (id, youtube_channel_id, display_name, stream_key, video_count, check_interval_hours, is_active, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'stopped')
        """, (
            channel_data.id,
            channel_data.youtube_channel_id,
            channel_data.display_name,
            channel_data.stream_key,
            channel_data.video_count,
            channel_data.check_interval_hours,
            1 if channel_data.is_active else 0
        ))

    # Trigger initial sync job for new channel
    try:
        sync_channel_job(channel_data.id)
        if channel_data.is_active:
            stream_manager.start_stream(channel_data.id)
    except Exception as e:
        logger.error(f"Error during initial setup for new channel {channel_data.id}: {e}")

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/channels/{id}/edit", response_class=HTMLResponse)
async def edit_channel_page(request: Request, id: str):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with get_db() as conn:
        channel_row = conn.execute("SELECT * FROM channels WHERE id = ?", (id,)).fetchone()
        if not channel_row:
            raise HTTPException(status_code=404, detail="Channel not found")
        channel = dict(channel_row)

    return templates.TemplateResponse(request=request, name="channel_form.html", context={
        "is_edit": True,
        "channel": channel
    })


@router.post("/channels/{id}/edit", response_class=HTMLResponse)
async def update_channel(
    request: Request,
    id: str,
    youtube_channel_id: str = Form(...),
    display_name: str = Form(...),
    stream_key: str = Form(...),
    video_count: int = Form(20),
    check_interval_hours: int = Form(6),
    is_active: Optional[str] = Form(None)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    is_active_bool = is_active is not None

    with get_db() as conn:
        conn.execute("""
            UPDATE channels
            SET youtube_channel_id = ?,
                display_name = ?,
                stream_key = ?,
                video_count = ?,
                check_interval_hours = ?,
                is_active = ?
            WHERE id = ?
        """, (
            youtube_channel_id,
            display_name,
            stream_key,
            video_count,
            check_interval_hours,
            1 if is_active_bool else 0,
            id
        ))

    if not is_active_bool and stream_manager.is_running(id):
        stream_manager.stop_stream(id)

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{id}/delete")
async def delete_channel(request: Request, id: str):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    # 1. Stop stream if running
    stream_manager.stop_stream(id)

    # 2. Delete database record
    with get_db() as conn:
        conn.execute("DELETE FROM channels WHERE id = ?", (id,))

    # 3. Clean up directory on disk
    data_dir = os.getenv("DATA_DIR", "./data")
    channel_dir = os.path.join(data_dir, id)
    if os.path.exists(channel_dir):
        try:
            shutil.rmtree(channel_dir)
        except Exception as e:
            logger.warning(f"Failed to remove directory {channel_dir}: {e}")

    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
