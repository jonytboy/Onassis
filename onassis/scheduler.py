"""Daily scheduler.

Wraps the lightweight ``schedule`` library so the orchestrator's daily
pipeline fires once per day at the configured time. Kept separate from
the orchestrator so the scheduling mechanism (cron, APScheduler, a cloud
trigger, ...) can change without touching pipeline logic.
"""

from __future__ import annotations

import time

import schedule

from onassis.config import Config
from onassis.logger import get_logger
from onassis.orchestrator import Orchestrator

log = get_logger(__name__)


class DailyScheduler:
    """Runs the orchestrator's daily pipeline at a fixed local time."""

    def __init__(self, config: Config, orchestrator: Orchestrator) -> None:
        self.config = config
        self.orchestrator = orchestrator

    def start(self) -> None:
        """Block forever, running the pipeline daily at ``config.run_at``.

        If ``scheduler.run_on_start`` is true, also runs it once now.
        """
        run_at = self.config.run_at
        schedule.every().day.at(run_at).do(self._job)
        log.info("Scheduler armed — daily run at %s (local time).", run_at)

        if self.config.run_on_start:
            log.info("run_on_start=true — running pipeline once now.")
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
            self.orchestrator.run_daily()
        except Exception:
            log.exception("Daily pipeline raised — will try again next run.")
