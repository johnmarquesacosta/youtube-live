import os
import json
import shutil
import logging
from typing import Optional
from fastapi import APIRouter, Request, Form, Depends, HTTPException, status, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from starlette.responses import Response
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


def _cookies_info() -> dict:
    """Return cookies file status info."""
    data_dir = os.getenv("DATA_DIR", "./data")
    cookie_file = os.path.join(data_dir, "cookies.txt")
    has_cookies = os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 0
    cookies_size = ""
    if has_cookies:
        size = os.path.getsize(cookie_file)
        if size > 1024:
            cookies_size = f"{size / 1024:.1f} KB"
        else:
            cookies_size = f"{size} bytes"
    return {"has_cookies": has_cookies, "cookies_size": cookies_size}


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    info = _cookies_info()
    return templates.TemplateResponse(request=request, name="settings.html", context=info)


@router.post("/settings/cookies", response_class=HTMLResponse)
async def upload_cookies(request: Request, cookies_file: UploadFile = File(...)):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    data_dir = os.getenv("DATA_DIR", "./data")
    os.makedirs(data_dir, exist_ok=True)
    cookie_path = os.path.join(data_dir, "cookies.txt")

    try:
        content = await cookies_file.read()
        text = content.decode("utf-8", errors="replace")

        # Basic validation: Netscape cookies should have the header or tab-separated lines
        if not text.strip():
            info = _cookies_info()
            info["error"] = "Uploaded file is empty."
            return templates.TemplateResponse(request=request, name="settings.html", context=info,
                                              status_code=status.HTTP_400_BAD_REQUEST)

        with open(cookie_path, "w", encoding="utf-8") as f:
            f.write(text)

        logger.info(f"Cookies file uploaded successfully ({len(content)} bytes)")
        info = _cookies_info()
        info["success"] = "Cookies uploaded successfully! New downloads will use authentication."
        return templates.TemplateResponse(request=request, name="settings.html", context=info)

    except Exception as e:
        logger.error(f"Error uploading cookies: {e}")
        info = _cookies_info()
        info["error"] = f"Error saving cookies: {e}"
        return templates.TemplateResponse(request=request, name="settings.html", context=info,
                                          status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


@router.post("/settings/cookies/delete")
async def delete_cookies(request: Request):
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    data_dir = os.getenv("DATA_DIR", "./data")
    cookie_path = os.path.join(data_dir, "cookies.txt")
    if os.path.exists(cookie_path):
        os.remove(cookie_path)
        logger.info("Cookies file deleted.")

    return RedirectResponse(url="/settings", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/settings/export")
async def export_channels(request: Request):
    """Export all channel configurations as a JSON file download."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM channels ORDER BY created_at").fetchall()
        channels = []
        for row in rows:
            channels.append({
                "id": row["id"],
                "youtube_channel_id": row["youtube_channel_id"],
                "display_name": row["display_name"],
                "stream_key": row["stream_key"],
                "video_count": row["video_count"],
                "check_interval_hours": row["check_interval_hours"],
                "is_active": bool(row["is_active"]),
            })

    export_data = json.dumps({"channels": channels}, indent=2, ensure_ascii=False)

    return Response(
        content=export_data,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=yt-live-channels.json"}
    )


@router.post("/settings/import", response_class=HTMLResponse)
async def import_channels(request: Request, config_file: UploadFile = File(...)):
    """Import channel configurations from a previously exported JSON file."""
    if not is_authenticated(request):
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)

    try:
        content = await config_file.read()
        text = content.decode("utf-8", errors="replace")
        data = json.loads(text)
        channels_list = data.get("channels", [])

        if not channels_list:
            info = _cookies_info()
            info["error"] = "JSON file contains no channels."
            return templates.TemplateResponse(request=request, name="settings.html", context=info,
                                              status_code=status.HTTP_400_BAD_REQUEST)

        imported = 0
        skipped = 0
        with get_db() as conn:
            for ch in channels_list:
                ch_id = ch.get("id", "").strip()
                if not ch_id:
                    skipped += 1
                    continue

                existing = conn.execute("SELECT id FROM channels WHERE id = ?", (ch_id,)).fetchone()
                if existing:
                    # Update existing channel config
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
                        ch.get("youtube_channel_id", ""),
                        ch.get("display_name", ch_id),
                        ch.get("stream_key", ""),
                        ch.get("video_count", 20),
                        ch.get("check_interval_hours", 6),
                        1 if ch.get("is_active", True) else 0,
                        ch_id
                    ))
                else:
                    conn.execute("""
                        INSERT INTO channels (id, youtube_channel_id, display_name, stream_key, video_count, check_interval_hours, is_active, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'stopped')
                    """, (
                        ch_id,
                        ch.get("youtube_channel_id", ""),
                        ch.get("display_name", ch_id),
                        ch.get("stream_key", ""),
                        ch.get("video_count", 20),
                        ch.get("check_interval_hours", 6),
                        1 if ch.get("is_active", True) else 0
                    ))
                imported += 1

        logger.info(f"Imported {imported} channels, skipped {skipped}")
        info = _cookies_info()
        info["success"] = f"Imported {imported} channel(s) successfully!" + (f" ({skipped} skipped)" if skipped else "")
        return templates.TemplateResponse(request=request, name="settings.html", context=info)

    except json.JSONDecodeError:
        info = _cookies_info()
        info["error"] = "Invalid JSON file. Please upload a file exported from this app."
        return templates.TemplateResponse(request=request, name="settings.html", context=info,
                                          status_code=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        logger.error(f"Error importing channels: {e}")
        info = _cookies_info()
        info["error"] = f"Import error: {e}"
        return templates.TemplateResponse(request=request, name="settings.html", context=info,
                                          status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
