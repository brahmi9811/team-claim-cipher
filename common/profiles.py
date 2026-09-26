"""Harness profile read/write and live ``watch`` via change streams.

``watch`` prefers a MongoDB change stream on ``harness_profiles``. If the
cluster/tier doesn't support it, falls back to polling every 5 seconds
(the 11:30 stub behavior, still useful).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from common import db as dbmod
from common.models import default_harness_profile

log = logging.getLogger(__name__)


def get_profile(insurer: str) -> dict[str, Any]:
    """Return the harness profile for ``insurer``, creating a default if missing."""
    try:
        db = dbmod.get_db("agent_worker")
        profile = db[dbmod.HARNESS_PROFILES].find_one({"_id": insurer})
        if profile is None:
            profile = default_harness_profile(insurer)
            db[dbmod.HARNESS_PROFILES].insert_one(profile)
        return profile
    except Exception as exc:  # noqa: BLE001 — allow offline/local use
        log.debug("get_profile falling back to default (%s)", exc)
        return default_harness_profile(insurer)


def save_profile(profile: dict[str, Any], event: dict | None = None) -> None:
    """Upsert a harness profile. Optionally stamp ``last_event_id`` from ``event``."""
    if event is not None:
        profile = dict(profile, last_event_id=event["_id"])
    db = dbmod.get_db("agent_worker")
    db[dbmod.HARNESS_PROFILES].replace_one(
        {"_id": profile["_id"]}, profile, upsert=True
    )


async def watch(
    callback: Callable[[dict], None],
    *,
    poll_interval: float = 5.0,
) -> None:
    """Push every harness-profile change to ``callback(profile)``.

    Tries a change stream first; on failure, polls every ``poll_interval``
    seconds and fires when ``version`` changes.
    """
    try:
        await _watch_change_stream(callback)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Change stream unavailable (%s); polling every %.1fs", exc, poll_interval
        )
        await _watch_poll(callback, poll_interval=poll_interval)


async def _watch_change_stream(callback: Callable[[dict], None]) -> None:
    db = dbmod.get_db("agent_worker")
    coll = db[dbmod.HARNESS_PROFILES]
    # fullDocument="updateLookup" so updates include the whole profile
    pipeline = [{"$match": {"operationType": {"$in": ["insert", "update", "replace"]}}}]
    with coll.watch(pipeline, full_document="updateLookup") as stream:
        # Run the blocking next() in a thread so we don't block the event loop
        loop = asyncio.get_running_loop()
        while True:
            change = await loop.run_in_executor(None, stream.next)
            doc = change.get("fullDocument")
            if doc:
                callback(doc)


async def _watch_poll(
    callback: Callable[[dict], None], *, poll_interval: float
) -> None:
    seen_versions: dict[str, int] = {}
    while True:
        try:
            db = dbmod.get_db("agent_worker")
            for profile in db[dbmod.HARNESS_PROFILES].find():
                last = seen_versions.get(profile["_id"])
                if last != profile.get("version"):
                    seen_versions[profile["_id"]] = profile.get("version", 0)
                    callback(profile)
        except Exception as exc:  # noqa: BLE001
            log.debug("profile poll error: %s", exc)
        await asyncio.sleep(poll_interval)


def watch_sync(
    callback: Callable[[dict], None],
    *,
    poll_interval: float = 5.0,
    stop_after: float | None = None,
) -> None:
    """Blocking poll-based watch for non-async callers (scripts, tests)."""
    seen_versions: dict[str, int] = {}
    started = time.monotonic()
    while True:
        try:
            db = dbmod.get_db("agent_worker")
            for profile in db[dbmod.HARNESS_PROFILES].find():
                last = seen_versions.get(profile["_id"])
                if last != profile.get("version"):
                    seen_versions[profile["_id"]] = profile.get("version", 0)
                    callback(profile)
        except Exception as exc:  # noqa: BLE001
            log.debug("profile poll error: %s", exc)
        if stop_after is not None and (time.monotonic() - started) >= stop_after:
            return
        time.sleep(poll_interval)
