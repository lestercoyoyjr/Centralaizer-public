"""
LocalMem entrypoint.
Starts the UI (port 3001) in a subprocess, then the MCP server (port 3000) in main process.
Using subprocess instead of threads avoids uvicorn circular-import conflicts.
"""
import sys
import logging
import subprocess
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from apscheduler.schedulers.background import BackgroundScheduler
from config.settings import settings
from core.storage.database import init_db
from core.memory.engine import run_decay, promote_skills

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("localmem")


_HOOK = str(Path(__file__).parent / "scripts" / "session_hook.py")


def _manager_tick():
    archived = run_decay()
    promoted = promote_skills()
    if archived or promoted:
        log.info("Manager: archived=%d promoted=%d", archived, promoted)
    # sweep newly-ended Claude Code sessions into the hub — catches sessions closed
    # abruptly (terminal/tab killed), which never fire the SessionEnd hook.
    try:
        subprocess.run([sys.executable, _HOOK, "--sweep"], timeout=300, check=False)
    except Exception as e:
        log.warning("session sweep failed: %s", e)


def main():
    log.info("Initialising local storage…")
    init_db()
    log.info("Storage ready: %s", settings.data_dir)

    # Memory manager scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_job(_manager_tick, "interval",
                      minutes=settings.manager_interval_minutes, id="manager")
    scheduler.start()
    log.info("Memory manager every %d min", settings.manager_interval_minutes)

    # Start UI as a separate subprocess (avoids uvicorn threading conflicts)
    ui_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "ui.app:app",
         "--host", settings.ui_host,
         "--port", str(settings.ui_port),
         "--log-level", "warning"],
        cwd=str(Path(__file__).parent),
    )
    log.info("Memory Viewer → http://%s:%d", settings.ui_host, settings.ui_port)

    # MCP server in main process
    log.info("MCP server → http://%s:%d/mcp", settings.mcp_host, settings.mcp_port)
    try:
        from core.mcp.server import run_server
        run_server()
    finally:
        ui_proc.terminate()
        scheduler.shutdown()


if __name__ == "__main__":
    main()
