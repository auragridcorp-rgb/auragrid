"""AurAgrid fetcher: runs every job in jobs.py on its interval and stores the results.

    python fetcher.py              # run forever (systemd: auragrid-fetcher.service)
    python fetcher.py --once       # run every enabled job once, then exit

Rules it follows
- A snapshot is written only after a successful fetch; failures are recorded
  in fetch_status and never touch stored data.
- Polite pacing: each job runs on its own interval with +/-10% jitter; after a
  failure it retries after 1 min, doubling up to the job's normal interval.
- Housekeeping: prune raw rows past retention hourly, VACUUM daily, warn when
  the database passes its size budget.
- It only reads upstream sources and writes the feed database. It has no access
  to prices, Aperture, payments or its own code paths (see the systemd unit).
"""
import logging
import os
import random
import signal
import sys
import threading
import time

from jobs import all_jobs
from store import Store

log = logging.getLogger("auragrid.fetcher")

PRUNE_EVERY = 3600
VACUUM_EVERY = 86400
TICK = 5


class Scheduler:
    def __init__(self, store: Store, jobs: list, clock=time.time, rng=random.random):
        self.store, self.clock, self.rng = store, clock, rng
        self.jobs = [j for j in jobs if j.enabled()]
        skipped = [j.key for j in jobs if not j.enabled()]
        if skipped:
            log.warning("Skipping jobs without their required settings: %s", ", ".join(skipped))
        now = clock()
        self.next_run = {j.key: now for j in self.jobs}
        self.backoff = {j.key: 0 for j in self.jobs}
        self.last_prune = self.last_vacuum = now

    def _jitter(self, seconds: float) -> float:
        return seconds * (0.9 + 0.2 * self.rng())

    def run_job(self, job) -> bool:
        started = self.clock()
        try:
            data = job.fetch()
            import feeds
            as_of = feeds.as_of_for(job.key, data) or "unknown"
            new = self.store.save(job.key, data, as_of)
            self.backoff[job.key] = 0
            self.next_run[job.key] = started + self._jitter(job.interval)
            log.info("%s ok (as_of %s%s) in %.1fs", job.key, as_of, "" if new else ", unchanged", self.clock() - started)
            return True
        except Exception as exc:
            self.store.record_failure(job.key, "%s: %s" % (type(exc).__name__, exc))
            delay = min(job.interval, 60 * 2 ** self.backoff[job.key])
            self.backoff[job.key] += 1
            self.next_run[job.key] = started + delay
            log.warning("%s failed (%s: %s); retry in %ds", job.key, type(exc).__name__, exc, delay)
            return False

    def housekeeping(self) -> None:
        now = self.clock()
        if now - self.last_prune >= PRUNE_EVERY:
            removed = self.store.prune()
            self.store.over_budget()
            self.last_prune = now
            if removed:
                log.info("Pruned %d raw snapshots past retention", removed)
        if now - self.last_vacuum >= VACUUM_EVERY:
            self.store.vacuum()
            self.last_vacuum = now
            log.info("VACUUM done; database %.1f MB", self.store.size_bytes() / 2**20)

    def tick(self) -> None:
        now = self.clock()
        for job in self.jobs:
            if now >= self.next_run[job.key]:
                self.run_job(job)
        self.housekeeping()


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    store = Store()
    sched = Scheduler(store, all_jobs())
    if "--once" in argv:
        ok = [sched.run_job(j) for j in sched.jobs]
        return 0 if all(ok) else 1
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    log.info("Fetcher started: %s", ", ".join(j.key for j in sched.jobs))
    while not stop.is_set():
        try:
            sched.tick()
        except Exception:
            log.exception("Scheduler tick failed")
        stop.wait(TICK)
    log.info("Fetcher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
