"""In-app marketing scheduler — UI-controlled cadence, no server crontab.

The operator sets *how often* the marketing push runs from the dashboard
(Business Settings → "Run marketing every N hours"); this background thread,
started when the API serves, fires :meth:`DailyCycle.run_marketing` on that
cadence. It replaces editing the OS crontab: change the number in the UI and the
next tick honours it.

Design:
* One daemon thread, checks every ``interval_s`` (default 60s) whether a run is
  due (``now - last_run >= every_hours``). The last-run timestamp is persisted in
  the DB (``system.last_marketing_run``) so a restart doesn't double-fire.
* ``every_hours == 0`` means "off / use an external cron" — the thread idles.
* Guarded: a failing run is logged and the loop continues; it never crashes the
  server or blocks requests.

It is started explicitly from the serve path (``main.py --serve``), never from
``create_app``, so tests that build the app do not spawn a scheduler thread.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from onassis.business_settings import BusinessSettings
from onassis.logger import get_logger

log = get_logger(__name__)

_LAST_RUN_KEY = "system.last_marketing_run"


class MarketingScheduler:
    def __init__(self, config: Any, db: Any, daily: Any = None) -> None:
        self.config = config
        self.db = db
        if daily is None:
            from onassis.daily_cycle import DailyCycle
            daily = DailyCycle(config, db)
        self.daily = daily
        self.settings = BusinessSettings(db, config)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # --- Config / state --------------------------------------------

    def _every_hours(self) -> int:
        try:
            return int(self.settings.get("marketing_every_hours"))
        except Exception:
            return 0

    def _last_run(self) -> str | None:
        try:
            return self.db.get_setting(_LAST_RUN_KEY, None)
        except Exception:
            return None

    def _mark_run(self, when: datetime) -> None:
        try:
            self.db.set_setting(_LAST_RUN_KEY, when.isoformat())
        except Exception:
            log.debug("could not persist last marketing run", exc_info=True)

    def _due(self, now: datetime) -> bool:
        hours = self._every_hours()
        if hours <= 0:
            return False
        last = self._last_run()
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        return (now - last_dt) >= timedelta(hours=hours)

    def status(self) -> dict[str, Any]:
        hours = self._every_hours()
        last = self._last_run()
        next_run = None
        if hours > 0 and last:
            try:
                last_dt = datetime.fromisoformat(last)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                next_run = (last_dt + timedelta(hours=hours)).isoformat()
            except Exception:
                next_run = None
        return {"enabled": hours > 0, "every_hours": hours,
                "last_run": last, "next_run": next_run,
                "running": bool(self._thread and self._thread.is_alive())}

    # --- Lifecycle -------------------------------------------------

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        if not self._due(now):
            return
        log.info("In-app scheduler: marketing run due (every %dh) — firing.",
                 self._every_hours())
        self._mark_run(now)          # mark before running so a long run can't double-fire
        try:
            self.daily.run_marketing()
        except Exception:
            log.exception("Scheduled marketing run failed — will retry next cadence.")

    def start(self, interval_s: int = 60) -> None:
        if self._thread and self._thread.is_alive():
            return
        # Don't fire immediately on a fresh boot — wait a full cadence.
        if not self._last_run():
            self._mark_run(datetime.now(timezone.utc))

        def _loop() -> None:
            while not self._stop.wait(interval_s):
                try:
                    self._tick()
                except Exception:
                    log.exception("scheduler tick failed")

        self._thread = threading.Thread(target=_loop, name="marketing-scheduler",
                                        daemon=True)
        self._thread.start()
        hours = self._every_hours()
        log.info("In-app marketing scheduler started (every %s, checks every %ds).",
                 f"{hours}h" if hours > 0 else "off", interval_s)

    def stop(self) -> None:
        self._stop.set()
