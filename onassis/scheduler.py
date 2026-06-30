"""Daily scheduler.

Wraps the lightweight ``schedule`` library so the single product-first
production workflow (:class:`~onassis.daily_cycle.DailyCycle`) fires once per
day at the configured time. Kept separate from the cycle so the scheduling
mechanism (cron, APScheduler, a cloud trigger, ...) can change without touching
workflow logic.
"""

from __future__ import annotations

import time

import schedule

from onassis.config import Config
from onassis.daily_cycle import DailyCycle
from onassis.logger import get_logger

log = get_logger(__name__)


class DailyScheduler:
    """Runs the product-first Daily Cycle at a fixed local time."""

    def __init__(self, config: Config, cycle: DailyCycle) -> None:
        self.config = config
        self.cycle = cycle

    def start(self) -> None:
        """Block forever, running the cycle daily at ``config.run_at``.

        If ``scheduler.run_on_start`` is true, also runs it once now.
        """
        run_at = self.config.run_at
        schedule.every().day.at(run_at).do(self._job)
        log.info("Scheduler armed — daily run at %s (local time).", run_at)

        if self.config.run_on_start:
            log.info("run_on_start=true — running the cycle once now.")
            self._job()

        log.info("Entering scheduler loop. Press Ctrl+C to stop.")
        try:
            while True:
                schedule.run_pending()
                time.sleep(30)
        except KeyboardInterrupt:
            log.info("Scheduler stopped by user.")

    def _job(self) -> None:
        """Single scheduled invocation, guarded so one bad day can't kill the loop."""
        try:
            self.cycle.run("production")
        except Exception:
            log.exception("Daily cycle raised — will try again next run.")
