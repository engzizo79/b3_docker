"""Background chain-health monitor.

Runs as a loop inside the backend process (FastAPI lifespan).
Polls getblockchaininfo, detects stalls, compares height with the
explorer, records alerts, and optionally signals recovery to the
entrypoint supervisor via a command file."""

import asyncio
import logging
import time
from pathlib import Path

import httpx

from app import db
from app.config import Settings
from app.rpc import B3RPCClient

logger = logging.getLogger("b3hive.monitor")


def _read_progress(settings: Settings) -> dict | None:
    """Read the setup-wizard bootstrap progress JSON, if present."""
    import json
    p = Path(settings.bootstrap_progress_file)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


class ChainMonitor:
    """Periodic chain-health checker."""

    def __init__(self, settings: Settings, rpc: B3RPCClient) -> None:
        self.settings = settings
        self.rpc = rpc
        self._task: asyncio.Task | None = None
        self._last_blocks: int | None = None
        self._last_ts: float | None = None
        self._stall_count: int = 0
        self._recovery_triggered: bool = False

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info("monitor started (interval=%ds, level=%s)",
                        self.settings.monitor_interval, self.settings.stall_level)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("monitor stopped")

    async def _run(self) -> None:
        while True:
            try:
                await self._check()
            except Exception as exc:
                logger.error("monitor check failed: %s", exc)
            await asyncio.sleep(self.settings.monitor_interval)

    async def _check(self) -> None:
        # Pause during an active setup-wizard bootstrap: the daemon is
        # intentionally stopped then; stall detection would false-fire.
        prog = _read_progress(self.settings)
        if prog and prog.get("phase") in ("stopping", "downloading",
                                           "verifying", "extracting"):
            logger.info("monitor paused during bootstrap (phase=%s)",
                        prog.get("phase"))
            return
        # Daemon deferred (first UI run, wizard pending): the wizard holds the node
        # down until a sync method is chosen; RPC is down by design.
        if Path(self.settings.daemon_deferred_file).is_file():
            logger.debug("monitor paused: daemon deferred (setup pending)")
            return
        info = await self.rpc.call("getblockchaininfo")
        blocks = info.get("blocks", 0)
        now = time.time()

        # Stall detection: block count has not increased in N minutes
        if self._last_blocks is not None and blocks <= self._last_blocks:
            elapsed = now - (self._last_ts or now)
            if elapsed >= self.settings.stall_alert_minutes * 60:
                self._stall_count += 1
                msg = (f"Chain stall: block {blocks} unchanged for "
                       f"{int(elapsed/60)} min (stall #{self._stall_count})")
                db.alert_add(self.settings.db_path, "stall", msg)
                logger.warning(msg)
                await self._notify_webhook("stall", msg)
                await self._maybe_recover()
                self._last_ts = now  # reset timer to avoid flood
        else:
            self._stall_count = 0

        self._last_blocks = blocks
        self._last_ts = now

        # Explorer sync-lag comparison
        await self._check_explorer_lag(blocks)

    async def _check_explorer_lag(self, local_blocks: int) -> None:
        url = self.settings.explorer_url.rstrip("/")
        if not url.startswith("http"):
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{url}/api/blocks/tip/height")
                if r.status_code == 200:
                    explorer_blocks = int(r.text.strip())
                    try:
                        tip_path = Path(self.settings.explorer_tip_file)
                        tip_path.parent.mkdir(parents=True, exist_ok=True)
                        tip_path.write_text(str(explorer_blocks))
                    except Exception:
                        pass
                    lag = explorer_blocks - local_blocks
                    if lag > 10:
                        msg = (f"Sync lag: local={local_blocks} explorer={explorer_blocks} "
                               f"({lag} blocks behind)")
                        db.alert_add(self.settings.db_path, "lag", msg)
                        logger.warning(msg)
                        await self._notify_webhook("lag", msg)
        except Exception as exc:
            logger.debug("explorer check failed: %s", exc)

    async def _maybe_recover(self) -> None:
        if self._recovery_triggered:
            return
        level = self.settings.stall_level
        if level == "alert":
            return  # alert only - no automatic action
        if level in ("restart", "reindex"):
            cmd = "restart" if level == "restart" else "reindex"
            cmd_file = Path(self.settings.recovery_cmd_file)
            try:
                cmd_file.parent.mkdir(parents=True, exist_ok=True)
                cmd_file.write_text(cmd)
                self._recovery_triggered = True
                msg = f"Recovery: wrote {cmd} to {cmd_file}"
                db.alert_add(self.settings.db_path, "recovery", msg)
                logger.info(msg)
                await self._notify_webhook("recovery", msg)
            except Exception as exc:
                logger.error("failed to write recovery cmd: %s", exc)

    async def _notify_webhook(self, event: str, message: str) -> None:
        url = self.settings.webhook_url
        if not url:
            return
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                await client.post(url, json={"event": event, "message": message})
        except Exception as exc:
            logger.debug("webhook failed: %s", exc)


# Singleton (created in main.py lifespan)
monitor: ChainMonitor | None = None


def start_monitor(settings: Settings, rpc: B3RPCClient) -> ChainMonitor:
    global monitor
    monitor = ChainMonitor(settings, rpc)
    monitor.start()
    return monitor


async def stop_monitor() -> None:
    global monitor
    if monitor is not None:
        await monitor.stop()
        monitor = None
