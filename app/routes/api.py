import os
import logging
from fastapi import APIRouter, Request, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth import is_authenticated
from app.db import get_db
from app.services.stream_manager import stream_manager
from app.services.scheduler import sync_channel_job

logger = logging.getLogger(__name__)

templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)

router = APIRouter(prefix="/channels")


def get_channel_or_404(channel_id: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM channels WHERE id = ?", (channel_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Channel '{channel_id}' not found")
        return dict(row)


def render_channel_card(request: Request, channel_id: str) -> HTMLResponse:
    channel = get_channel_or_404(channel_id)
    return templates.TemplateResponse(
        request=request,
        name="partials/channel_card.html",
        context={"channel": channel}
    )


@router.post("/{id}/start", response_class=HTMLResponse)
async def start_channel(request: Request, id: str):
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": "/login"})

    # Ensure channel exists
    channel = get_channel_or_404(id)

    # Activate in DB
    with get_db() as conn:
        conn.execute("UPDATE channels SET is_active = 1 WHERE id = ?", (id,))

    # Trigger sync to ensure playlist exists
    try:
        sync_channel_job(id)
    except Exception as e:
        logger.error(f"Sync error during start of {id}: {e}")

    # Start FFmpeg stream
    stream_manager.start_stream(id)

    return render_channel_card(request, id)


@router.post("/{id}/stop", response_class=HTMLResponse)
async def stop_channel(request: Request, id: str):
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": "/login"})

    get_channel_or_404(id)

    # Stop stream process & deactivate
    stream_manager.stop_stream(id)

    return render_channel_card(request, id)


@router.post("/{id}/refresh", response_class=HTMLResponse)
async def refresh_channel(request: Request, id: str):
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": "/login"})

    get_channel_or_404(id)

    # Trigger instant sync
    try:
        sync_channel_job(id)
    except Exception as e:
        logger.error(f"Error refreshing channel {id}: {e}")

    return render_channel_card(request, id)


@router.get("/{id}/card", response_class=HTMLResponse)
async def get_channel_card(request: Request, id: str):
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, headers={"HX-Redirect": "/login"})

    return render_channel_card(request, id)
