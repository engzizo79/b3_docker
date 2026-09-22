"""Background wallet-event watcher: polls listtransactions for new
receives/sends/stakes and feeds them into the shared notifier (app/notifier.py).

One poller catches every wallet-affecting code path (manual send, batch,
consolidation, autostake top-up, unstake, sendall) uniformly, instead of
instrumenting each of them individually.

Mirrors the ChainMonitor background-task shape in app/monitor.py."""

import asyncio
import logging
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from app import db, notifier
from app.rpc import B3RPCClient

logger = logging.getLogger("b3hive.wallet_monitor")

# Node tx categories -> the user-facing notification bucket. Grouping
# stake/generate/immature together (same as frontend/js/core/state.js does
# for icons) means the immature->mature transition of one reward doesn't
# fire twice: the dedupe key below is keyed on the bucket, not the raw
# category, so whichever one is seen first "claims" that txid:vout.
_BUCKETS = {
    "receive": "received",
    "send": "sent",
    "stake": "stake",
    "generate": "stake",
    "immature": "stake",
}

POLL_LIMIT = 100
SEEN_TTL_SECONDS = 30 * 24 * 3600


def _dec(value) -> Decimal:
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError, InvalidOperation):
        return Decimal(0)


def _seen_key(tx: dict, bucket: str) -> str:
    return f"{tx.get('txid', '')}:{tx.get('vout', 0)}:{bucket}"


def _message(bucket: str, tx: dict, amount: Decimal) -> str:
    address = tx.get("address", "")
    if bucket == "received":
        return f"Received {amount} B3" + (f" to {address}" if address else "")
    if bucket == "sent":
        return f"Sent {amount} B3" + (f" to {address}" if address else "")
    return f"Staking reward: +{amount} B3"


class WalletMonitor:
    """Periodic wallet-activity checker feeding the shared notifier."""

    def __init__(self, settings, rpc: B3RPCClient) -> None:
        self.settings = settings
        self.rpc = rpc
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())
            logger.info("wallet monitor started (interval=%ds)", self.settings.notify_interval)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
            logger.info("wallet monitor stopped")

    async def _run(self) -> None:
        while True:
            try:
                await self._check()
            except Exception as exc:
                logger.error("wallet monitor check failed: %s", exc)
            await asyncio.sleep(self.settings.notify_interval)

    async def _check(self) -> None:
        # Digest flushing is RPC-independent - always run it, even if the
        # node/wallet below is unreachable.
        await notifier.flush_due(self.settings)

        # Same pauses as ChainMonitor: the daemon is intentionally down
        # during setup-wizard bootstrap / before the first sync method is
        # chosen, so RPC calls would just fail (or, worse, hit a stale
        # daemon on the way back up).
        if Path(self.settings.daemon_deferred_file).is_file():
            return

        try:
            wallets = await self.rpc.call("listwallets")
        except Exception:
            return  # node unreachable; try again next tick
        if not wallets:
            return  # no wallet loaded - nothing to watch yet

        try:
            txs = await self.rpc.call("listtransactions", "*", POLL_LIMIT, 0)
        except Exception as exc:
            logger.debug("wallet monitor: listtransactions failed: %s", exc)
            return

        dbp = self.settings.db_path

        if not db.notification_seeded(dbp):
            # First-ever pass: record everything currently present WITHOUT
            # notifying. Otherwise turning this on for an existing wallet
            # would instantly "discover" and notify for its entire history -
            # exactly the backfill-flood mistake app/monitor.py's sync-lag
            # check already avoids for explorer comparisons.
            for tx in txs:
                bucket = _BUCKETS.get(tx.get("category"))
                if bucket:
                    db.notification_seen_add(dbp, _seen_key(tx, bucket))
            db.notification_mark_seeded(dbp)
            return

        # Oldest first, so a burst notifies in the order it happened.
        for tx in reversed(txs):
            bucket = _BUCKETS.get(tx.get("category"))
            if bucket is None:
                continue
            key = _seen_key(tx, bucket)
            if db.notification_seen_has(dbp, key):
                continue
            db.notification_seen_add(dbp, key)
            amount = abs(_dec(tx.get("amount", 0)))
            await notifier.notify(self.settings, bucket, _message(bucket, tx, amount), amount=amount)

        db.notification_seen_prune(dbp, time.time() - SEEN_TTL_SECONDS)


# Singleton (created in main.py lifespan), mirrors app/monitor.py's `monitor`.
wallet_monitor: WalletMonitor | None = None


def start_wallet_monitor(settings, rpc: B3RPCClient) -> WalletMonitor:
    global wallet_monitor
    wallet_monitor = WalletMonitor(settings, rpc)
    wallet_monitor.start()
    return wallet_monitor


async def stop_wallet_monitor() -> None:
    global wallet_monitor
    if wallet_monitor is not None:
        await wallet_monitor.stop()
        wallet_monitor = None
