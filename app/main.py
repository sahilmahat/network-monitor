"""
main.py — FastAPI application entry point.

Wires together:
  - Pydantic-validated config loaded at startup (fail-fast)
  - Background scheduler that pings every N seconds
  - Background cleanup task that deletes old records daily
  - SQLite storage for results and incidents
  - REST API for the dashboard (filters DB results against current config)
  - Static file serving for the dashboard frontend
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.monitor import ping_all_targets
from app.config_schema import load_and_validate_config
from app.database import (
    init_db,
    insert_ping_result,
    get_latest_status,
    get_history,
    get_uptime_percentage,
    get_recent_incidents,
    cleanup_old_records,
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
# Background tasks
# ----------------------------------------------------------------------
async def monitor_loop(config) -> None:
    """
    Run forever: ping all targets, store results, sleep, repeat.
    Wraps each iteration in try/except so a single failure can't kill the loop.
    """
    interval = config.ping_interval_seconds
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
            log.exception(f"monitor_loop iteration failed: {e}")

        await asyncio.sleep(interval)


async def cleanup_loop(retention_days: int) -> None:
    """
    Run forever: sleep 24 hours, then delete old records.
    Sleeps first so we don't VACUUM on every startup.
    """
    log.info(f"Cleanup loop started, retention = {retention_days} days")
    while True:
        await asyncio.sleep(24 * 60 * 60)  # 24 hours
        try:
            result = cleanup_old_records(retention_days)
            log.info(f"Cleanup: deleted {result['deleted_pings']} old pings")
        except Exception as e:
            log.exception(f"cleanup_loop iteration failed: {e}")


# ----------------------------------------------------------------------
# Lifespan — startup/shutdown hooks
# ----------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    log.info("Starting CIPL Network Monitor...")
    init_db()
    log.info("Database initialised")

    # Validate config — crashes here if config.yaml is malformed
    config = load_and_validate_config()
    log.info(
        f"Config validated: {len(config.gateways)} gateways, "
        f"{len(config.vms)} VMs, retention {config.retention_days}d"
    )
    app.state.config = config

    # Launch background tasks
    monitor_task = asyncio.create_task(monitor_loop(config))
    cleanup_task = asyncio.create_task(cleanup_loop(config.retention_days))
    log.info("Background tasks scheduled")

    yield  # <-- application runs here

    # --- Shutdown ---
    log.info("Shutting down — cancelling background tasks")
    monitor_task.cancel()
    cleanup_task.cancel()
    for task in (monitor_task, cleanup_task):
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
# Helpers
# ----------------------------------------------------------------------
def _get_configured_hosts() -> set[str]:
    """
    Return the set of hosts currently in config (source of truth).
    Used to filter DB query results so removed targets don't appear on the dashboard.
    """
    config = app.state.config
    return {t.host for t in list(config.gateways) + list(config.vms)}


# ----------------------------------------------------------------------
# API endpoints
# ----------------------------------------------------------------------
@app.get("/healthz")
async def healthz():
    """Liveness probe — returns 200 if the service is alive."""
    return {"status": "ok"}


@app.get("/api/status")
async def api_status():
    """
    Latest ping result for every CURRENTLY CONFIGURED target.
    Hosts removed from config are filtered out, even if they have history in the DB.
    """
    configured = _get_configured_hosts()
    targets = [t for t in get_latest_status() if t["host"] in configured]
    return {
        "targets": targets,
        "count": len(targets),
    }


@app.get("/api/summary")
async def api_summary():
    """
    Aggregated dashboard stats — counts ONLY currently configured targets.
    Single endpoint for the dashboard's top bar — saves multiple round-trips.
    """
    configured = _get_configured_hosts()
    targets = [t for t in get_latest_status() if t["host"] in configured]
    incidents = [
        i for i in get_recent_incidents(hours=24)
        if i["host"] in configured
    ]

    total = len(targets)
    up = sum(1 for t in targets if t["is_up"])
    down = total - up

    gateways = [t for t in targets if t["type"] == "gateway"]
    vms = [t for t in targets if t["type"] == "vm"]

    return {
        "total_targets": total,
        "up": up,
        "down": down,
        "gateways": {
            "total": len(gateways),
            "up": sum(1 for t in gateways if t["is_up"]),
        },
        "vms": {
            "total": len(vms),
            "up": sum(1 for t in vms if t["is_up"]),
        },
        "incidents_24h": len(incidents),
        "last_updated": targets[0]["checked_at"] if targets else None,
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
    """
    Recent state-transition events (down/recovered).
    Filtered against current config so old removed hosts don't pollute the timeline.
    """
    if hours < 1 or hours > 720:
        raise HTTPException(status_code=400, detail="hours must be between 1 and 720")

    configured = _get_configured_hosts()
    all_incidents = get_recent_incidents(hours)
    incidents = [i for i in all_incidents if i["host"] in configured]

    return {
        "hours": hours,
        "incidents": incidents,
    }


# ----------------------------------------------------------------------
# Static frontend
# ----------------------------------------------------------------------
@app.get("/")
async def root():
    """Serve the dashboard if it exists, otherwise a placeholder."""
    index = Path("static/index.html")
    if index.exists():
        return FileResponse(index)
    return {
        "message": "CIPL Network Monitor is running",
        "dashboard": "Not yet built — coming on Day 3",
        "api_docs": "/docs",
    }


# Serve any other static files (CSS, JS)
if Path("static").exists():
    app.mount("/static", StaticFiles(directory="static"), name="static")
