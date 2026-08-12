import os
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.auth import SESSION_SECRET
from app.db import init_db, get_db
from app.routes.ui import router as ui_router
from app.routes.api import router as api_router
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.stream_manager import stream_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("app.main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks
    logger.info("Initializing SQLite database schema...")
    init_db()

    logger.info("Starting background scheduler...")
    start_scheduler()

    # Re-activate streams for active channels on startup
    with get_db() as conn:
        active_channels = conn.execute("SELECT id FROM channels WHERE is_active = 1").fetchall()

    logger.info(f"Restoring streams for {len(active_channels)} active channels...")
    for row in active_channels:
        ch_id = row["id"]
        try:
            stream_manager.start_stream(ch_id)
        except Exception as e:
            logger.error(f"Failed to restore stream for active channel {ch_id}: {e}")

    yield

    # Shutdown tasks
    logger.info("Stopping background scheduler...")
    stop_scheduler()

    logger.info("Terminating all active stream processes...")
    with get_db() as conn:
        all_channels = conn.execute("SELECT id FROM channels").fetchall()
    for row in all_channels:
        try:
            stream_manager.stop_stream(row["id"])
        except Exception as e:
            logger.warning(f"Error stopping stream for {row['id']} during shutdown: {e}")


app = FastAPI(
    title="YouTube Live Loop",
    description="Single-instance multi-channel YouTube Live stream looping service",
    lifespan=lifespan
)

# Session middleware
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Mount static directory
static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# Register routers
app.include_router(ui_router)
app.include_router(api_router)
