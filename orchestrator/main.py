"""Async orchestrator: claim-rate control, the per-claim worker, the
appeal-approval worker, and the Evolver's background cycle. docs/PLAN.md:
"one async Python process runs components 3 to 8 as workers, streaming about
2 claims per second (adjustable) so we control LLM cost", "The Evolver runs
every 5 minutes, or after every 50 new outcomes", and the agent "runs in a
loop all day".

Requires `MONGODB_URI` (see `.env.example`) -- every write goes through
`common.db`. Run standalone with `python -m orchestrator.main`: by default
it runs forever (Ctrl+C to stop), draining real claims from `claims` (once
A's `forge/` has loaded them) and topping up with freshly generated demo
claims whenever that queue is empty, so the loop never just stops. Set
`MAX_CLAIMS` to process a fixed number and exit instead (handy for a quick
check).
"""
from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from validator import lifecycle

from agents import evolver
from orchestrator import claims_source, config
from orchestrator.appeal_worker import file_approved_appeals
from orchestrator.db import coll
from orchestrator.pipeline import process_claim
from orchestrator.seed import seed_policies

logger = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(
        self,
        *,
        rate_per_sec: float = config.CLAIM_RATE_PER_SEC,
        evolver_cycle_seconds: float = config.EVOLVER_CYCLE_SECONDS,
        evolver_cycle_outcomes: int = config.EVOLVER_CYCLE_OUTCOMES,
        appeal_poll_seconds: float = config.APPEAL_POLL_SECONDS,
        batch_size: int = config.BATCH_SIZE,
        max_in_flight: int = config.MAX_IN_FLIGHT,
    ) -> None:
        self._rate_per_sec = rate_per_sec
        self._evolver_cycle_seconds = evolver_cycle_seconds
        self._evolver_cycle_outcomes = evolver_cycle_outcomes
        self._appeal_poll_seconds = appeal_poll_seconds
        self._batch_size = batch_size
        self._max_in_flight = max(1, max_in_flight)
        self._outcomes_since_evolve = 0
        self._started = 0
        self._processed = 0
        self._paid = 0
        self._evolve_lock = asyncio.Lock()

    async def run_forever(self, *, max_claims: int = 0) -> None:
        """Drain claims_source.next_batch() at the configured rate, forever
        (or until `max_claims` have been processed, if set). Runs two
        background tasks throughout: an Evolver cycle every
        `evolver_cycle_seconds` (also fired immediately whenever
        `evolver_cycle_outcomes` claims have been processed since the last
        one), and a poll that files any appeal a human approved."""
        background = [
            asyncio.create_task(self._time_based_evolver_loop()),
            asyncio.create_task(self._appeal_approval_loop()),
        ]
        # Claims are started at `rate_per_sec` but processed concurrently (up to
        # `max_in_flight` at once): the simulator takes 2.5-6 s per review, so one
        # claim at a time would cap the loop near 0.2 claims/s, and Payer B's bulk
        # denials only batch when claims arrive together.
        slots = asyncio.Semaphore(self._max_in_flight)
        in_flight: set[asyncio.Task] = set()
        try:
            while max_claims <= 0 or self._started < max_claims:
                claims = await asyncio.to_thread(claims_source.next_batch, self._batch_size)
                for claim in claims:
                    if 0 < max_claims <= self._started:
                        break
                    await slots.acquire()
                    self._started += 1
                    task = asyncio.create_task(self._process(claim, slots))
                    in_flight.add(task)
                    task.add_done_callback(in_flight.discard)
                    if self._rate_per_sec > 0:
                        await asyncio.sleep(1 / self._rate_per_sec)
            if in_flight:
                await asyncio.gather(*in_flight, return_exceptions=True)
        finally:
            for task in background:
                task.cancel()
            logger.info("Processed %d claims total (%d paid on first pass).", self._processed, self._paid)

    async def _process(self, claim: dict, slots: asyncio.Semaphore) -> None:
        try:
            # process_claim does blocking I/O (urllib/pymongo calls);
            # to_thread keeps the event loop free for other claims and background tasks.
            summary = await asyncio.to_thread(process_claim, claim)
            self._processed += 1
            self._paid += summary.get("status") == "paid"
            self._outcomes_since_evolve += 1
            if self._outcomes_since_evolve >= self._evolver_cycle_outcomes:
                await self._run_evolver_cycle()
        except Exception:  # noqa: BLE001 -- one bad claim (or evolver cycle) must not stop the loop
            logger.exception("claim %s failed", claim.get("_id"))
        finally:
            slots.release()

    async def _time_based_evolver_loop(self) -> None:
        # Background loops log and continue: an uncaught error would end the task for the rest of the run.
        while True:
            await asyncio.sleep(self._evolver_cycle_seconds)
            try:
                await self._run_evolver_cycle()
            except Exception:  # noqa: BLE001
                logger.exception("evolver cycle failed; retrying next cycle")

    async def _appeal_approval_loop(self) -> None:
        while True:
            await asyncio.sleep(self._appeal_poll_seconds)
            try:
                filed = await asyncio.to_thread(file_approved_appeals)
            except Exception:  # noqa: BLE001
                logger.exception("appeal approval poll failed; retrying")
                continue
            for appeal in filed:
                logger.info("[appeal_worker] filed %s -- %s", appeal["_id"], appeal["outcome"])

    async def _run_evolver_cycle(self) -> None:
        if self._evolve_lock.locked():  # concurrent claims can trigger it together; one cycle is enough
            return
        async with self._evolve_lock:
            await self._evolver_cycle()

    async def _evolver_cycle(self) -> None:
        self._outcomes_since_evolve = 0
        for insurer in config.INSURERS:
            event = await asyncio.to_thread(evolver.run_cycle, insurer)
            if event:
                logger.info("[evolver] %s: %s -- %s", insurer, event["type"], event["reason"])
            await asyncio.to_thread(lifecycle.retire_stale, insurer)


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # to_thread's default pool is small (cpu + 4); give every in-flight claim a thread.
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=config.MAX_IN_FLIGHT + 8))
    seed_policies()

    logger.info(
        "Starting: %.1f claims/sec, %s. Ctrl+C to stop.",
        config.CLAIM_RATE_PER_SEC,
        f"stopping after {config.MAX_CLAIMS} claims" if config.MAX_CLAIMS else "running forever",
    )
    orchestrator = Orchestrator()
    try:
        await orchestrator.run_forever(max_claims=config.MAX_CLAIMS)
    except asyncio.CancelledError:
        pass

    logger.info("Rules: %s", [(r["_id"], r["status"]) for r in coll("rules").find()])
    logger.info("Appeals: %s", [(a["_id"], a.get("outcome")) for a in coll("appeals").find()])


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        logger.info("Stopped.")
