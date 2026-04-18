from __future__ import annotations

import datetime
import logging
from typing import Callable

import timez

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, job_queue: Any) -> None:
        self._jq = job_queue

    def every(self, interval: int, callback: Callable, name: str) -> None:
        self._jq.run_repeating(callback, interval=interval, name=name, first=10)
        logger.info("Scheduled repeating job '%s' every %ds", name, interval)

    def daily_at(self, hh_mm: str, callback: Callable, name: str) -> None:
        h, m = map(int, hh_mm.split(":"))
        t = datetime.time(h, m, tzinfo=timez._tz)
        self._jq.run_daily(callback, time=t, name=name)
        logger.info("Scheduled daily job '%s' at %s %s", name, hh_mm, timez.tz_label())

    def cron(self, expr: str, callback: Callable, name: str) -> None:
        try:
            from croniter import croniter
        except ImportError:
            logger.error(
                "croniter not installed; cannot schedule '%s' with expr '%s'", name, expr
            )
            return

        async def _cron_job(context) -> None:
            try:
                await callback(context)
            except Exception:
                logger.exception("Cron job '%s' callback failed", name)
            finally:
                next_dt = timez.next_cron(expr)
                delay = (next_dt - timez.now()).total_seconds()
                context.job_queue.run_once(_cron_job, when=delay, name=name)
                logger.info(
                    "Cron job '%s' rescheduled for %s %s",
                    name, next_dt.strftime("%Y-%m-%d %H:%M:%S"), timez.tz_label(),
                )

        next_dt = timez.next_cron(expr)
        delay = (next_dt - timez.now()).total_seconds()
        self._jq.run_once(_cron_job, when=delay, name=name)
        logger.info(
            "Scheduled cron job '%s' (expr='%s') next at %s %s",
            name, expr, next_dt.strftime("%Y-%m-%d %H:%M:%S"), timez.tz_label(),
        )


# Satisfy type checker without a runtime import
from typing import Any  # noqa: E402
