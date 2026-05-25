"""
main.py — FastAPI application entry point.

Wires together:
  - Background scheduler that pings every N seconds
  - SQLite storage for results and incidents
  - REST API for the dashboard to query
  - Static file serving for the dashboard frontend
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.monitor import load_config, ping_all_targets
from app.database import (
    init_db,
    insert_ping_result,
    get_latest_status,
    get_history,
    get_uptime_percentage,
    get_recent_incidents,
)

# ----------------------------------------------------------------------
# Logging setup
# ----------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("cipl-monitor")


# ----------------------------------------------------------------------
# Background scheduler
# ----------------------------------------------------------------------
async def monitor_loop(config: dict) -> None:
    """
    Run forever: ping all targets, store results, sleep, repeat.
    Wraps each iteration in try/except so a single failure can't kill the loop.
    """
    interval = config.get("ping_interval_seconds", 30)
    log.info(f"Monitor loop started, interval = {interval}s")

    while True:
        try:
            results = ping_all_targets(config)
            for r in results:
                insert_ping_result(r)

            up = sum(1 for r in results if r["is_up"])
            down = len(results) - up
            log.info(f"Pinged {len(results)} targets: {up} up, {down} down")

        except Exception as e:
            # Catch-all: log and continue. Loop must never die.
            log.exception(f"monitor_loop iteration failed: {e}")

        await asyncio.sleep(interval)


# ----------------------------------------------------------------------
# Lifespan — startup/shutdown hooks (modern FastAPI pattern)
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    log.info("Starting CIPL Network Monitor...")
    init_db()
    log.info("Database initialised")

    config = load_config()
    app.state.config = config

    # Launch background scheduler as an asyncio task
    task = asyncio.create_task(monitor_loop(config))
    log.info("Background monitor scheduled")

    yield  # <-- application runs here

    # --- Shutdown ---
    log.info("Shutting down — cancelling monitor loop")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ----------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------
app = FastAPI(
    title="CIPL Network Monitor",
    description="Self-hosted monitoring for CIPL gateways and VMs",
    version="1.0.0",
    lifespan=lifespan,
)


# ----------------------------------------------------------------------
# API endpoints
# ----------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Liveness probe — returns 200 if the service is alive."""
    return {"status": "ok"}


@app.get("/api/status")
async def api_status():
    """Latest ping result for every monitored target."""
    targets = get_latest_status()
    return {
        "targets": targets,
        "count": len(targets),
    }


@app.get("/api/history/{host}")
async def api_history(host: str, hours: int = 24):
    """Ping history for one host over the last N hours."""
    if hours < 1 or hours > 720:  # max 30 days
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")
    return {
        "host": host,
        "hours": hours,
        "results": get_history(host, hours),
    }


@app.get("/api/uptime/{host}")
async def api_uptime(host: str, hours: int = 24):
    """Uptime percentage for one host over the last N hours."""
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")
    return get_uptime_percentage(host, hours)


@app.get("/api/incidents")
async def api_incidents(hours: int = 24):
    """Recent state-transition events (down/recovered)."""
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")
    return {
        "hours": hours,
        "incidents": get_recent_incidents(hours),
    }


# ----------------------------------------------------------------------
# Static frontend (will be populated on Day 3)
# ----------------------------------------------------------------------
@app.get("/")
async def root():
    """Serve the dashboard if it exists, otherwise a placeholder."""
    from pathlib import Path
    index = Path("static/index.html")
    if index.exists():
        return FileResponse(index)
    return {
        "message": "CIPL Network Monitor is running",
        "dashboard": "Not yet built — coming on Day 3",
        "api_docs": "/docs",
    }


# Serve any other static files (CSS, JS) once we add them
from pathlib import Path
if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")
