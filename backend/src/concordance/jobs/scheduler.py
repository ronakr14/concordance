"""The interval scheduler, which is twenty lines and lives inside the worker.

There is no cron container and no APScheduler. A scheduled job here is a kind, an
interval, and the last time it was enqueued; the worker asks what is due on
every pass of its loop and enqueues it. That is the whole mechanism, and it buys
three things a cron container does not:

- **It is one deployment.** `docker compose up` brings up a worker, and the
  schedule comes with it. Nothing has to be installed into an image, and no
  second process can drift out of sync with the code it triggers.
- **Catch-up is explicit.** A worker that was down over the weekend enqueues the
  missed run when it starts, because the scheduler asks "has this ever run, and
  how long ago" rather than "did the clock strike while I was watching". Cases
  that expired while nothing was running are transitioned on the next start.
- **Duplicate suppression is the queue's job, not the scheduler's.** `due()`
  proposes; the worker enqueues only when no job of that kind is already
  pending. Two workers therefore cannot both schedule the same daily sweep.

Idempotency still belongs to the handler. `expire_cases` transitions nothing on
a second run in the same day, so an extra enqueue is a wasted query rather than
a wrong answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

#: Once a day, a few minutes after midnight UTC in effect - the interval is
#: measured from the last run, not aligned to a wall clock, which is what makes
#: a restart harmless.
DAILY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    """One recurring job."""

    kind: str
    interval: timedelta
    payload: dict[str, Any] = field(default_factory=dict)
    #: Enqueue once at startup, before the first interval has elapsed. True for
    #: anything whose work piles up while nothing is running.
    catch_up: bool = True


@dataclass
class IntervalScheduler:
    """What is due, and when each kind last went out."""

    entries: list[ScheduledJob] = field(default_factory=list)
    last_run: dict[str, datetime] = field(default_factory=dict)
    started: bool = False

    def due(self, now: datetime | None = None) -> list[ScheduledJob]:
        """Every entry whose interval has elapsed, plus the startup catch-up."""
        moment = now or datetime.now(UTC)
        ready: list[ScheduledJob] = []
        for entry in self.entries:
            last = self.last_run.get(entry.kind)
            if last is None:
                if entry.catch_up or self.started:
                    ready.append(entry)
                continue
            if moment - last >= entry.interval:
                ready.append(entry)
        self.started = True
        return ready

    def mark(self, kind: str, now: datetime | None = None) -> None:
        self.last_run[kind] = now or datetime.now(UTC)

    def next_due(self, now: datetime | None = None) -> datetime | None:
        moment = now or datetime.now(UTC)
        times = [
            self.last_run.get(entry.kind, moment) + entry.interval for entry in self.entries
        ]
        return min(times) if times else None


def default_schedule() -> IntervalScheduler:
    """What the worker runs on its own: case expiry, daily, with catch-up (Q3)."""
    return IntervalScheduler(
        entries=[ScheduledJob(kind="expire_cases", interval=DAILY, payload={}, catch_up=True)]
    )


__all__ = ["DAILY", "IntervalScheduler", "ScheduledJob", "default_schedule"]
