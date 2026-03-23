"""Thrall Settlement Identity — Scoped wallet with operator ceiling.

Petty-cash mechanism: thrall can authorize credit operations up to
the rolling 24h ceiling without human approval.

v3.9 T4: Changed from midnight-UTC reset to rolling 24h window.
This fixes multi-day experiment stability — the old reset depended
on wall-clock midnight which failed inconsistently in Docker containers
without timezone config.

Config:
    [config.thrall.wallet]
    ceiling = 50.0          # max credits per rolling 24h window
"""

import logging
import time

from db import ThrallDB

logger = logging.getLogger("thrall.wallet")


class ThrallWallet:
    """Rolling-window spending authority for thrall settlement operations."""

    def __init__(self, db: ThrallDB, config: dict):
        self._db = db
        self._ceiling = float(config.get("ceiling", 50.0))
        self._window = float(config.get("window_seconds", 86400))  # 24h default
        self._enabled = config.get("enabled", True) and self._ceiling > 0
        if self._enabled:
            status = self.get_status()
            logger.info(
                f"WALLET_INIT ceiling={self._ceiling} "
                f"window={self._window:.0f}s "
                f"spent_in_window={status['spent_in_window']:.1f} "
                f"remaining={status['remaining']:.1f}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _window_start(self) -> float:
        """Start of the rolling window (now - window_seconds)."""
        return time.time() - self._window

    def can_spend(self, amount: float) -> bool:
        """Check if amount fits within remaining rolling window ceiling."""
        if not self._enabled:
            return False
        spent = self._db.get_daily_spend(self._window_start())
        return (spent + amount) <= self._ceiling

    def record_spend(self, amount: float, reference: str = "",
                     peer_pk: str = "", description: str = ""):
        """Record a spending event. Call after successful settlement proposal."""
        self._db.record_wallet_spend(amount, reference, peer_pk, description)
        logger.info(
            f"WALLET_SPEND amount={amount:.1f} ref={reference[:16]} "
            f"peer={peer_pk[:16]}")

    def get_status(self) -> dict:
        """Current wallet status."""
        spent = self._db.get_daily_spend(self._window_start())
        return {
            "ceiling": self._ceiling,
            "spent_in_window": round(spent, 2),
            "remaining": round(max(0, self._ceiling - spent), 2),
            "window_seconds": self._window,
        }

    def cleanup_old_spends(self, keep_hours: float = 48) -> int:
        """Delete spend records older than keep_hours. Returns count deleted."""
        cutoff = time.time() - (keep_hours * 3600)
        cur = self._db._conn.execute(
            "DELETE FROM thrall_wallet_spend WHERE timestamp < ?", (cutoff,))
        self._db._conn.commit()
        count = cur.rowcount
        if count:
            logger.debug(f"WALLET_CLEANUP deleted={count} older_than={keep_hours}h")
        return count
