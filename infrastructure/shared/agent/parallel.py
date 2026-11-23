"""Bounded parallel execution of independent model tasks."""

from concurrent.futures import ThreadPoolExecutor, wait
from contextvars import copy_context
import os


DEFAULT_PARALLEL_AGENTS = 6


def parallel_agents() -> int:
    """Concurrent independent model tasks (PARALLEL_AGENTS, default 6; 1 disables parallelism)."""
    try:
        value = int(os.getenv("PARALLEL_AGENTS") or DEFAULT_PARALLEL_AGENTS)
    except ValueError:
        value = DEFAULT_PARALLEL_AGENTS
    return max(1, value)


def run_parallel(worker, items, max_workers=None) -> dict:
    """Run worker(item) for every item concurrently and return {item: result or exception}.

    Each call runs in a copy of the caller's context so the session log, stage label
    and activity labels stay attached to the worker thread. Exceptions are returned,
    never raised, so one failed task cannot cancel the others; callers fall back to
    their sequential path for any item without a successful result.
    """
    items = list(items)
    if not items:
        return {}
    workers = min(max_workers or parallel_agents(), len(items))
    results = {}

    def guarded(item):
        try:
            return worker(item)
        except Exception as error:  # noqa: BLE001 - reported to the caller per item
            return error

    if workers == 1:
        return {item: guarded(item) for item in items}
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {item: pool.submit(copy_context().run, guarded, item) for item in items}
        pending = set(futures.values())
        while pending:
            # Poll so Ctrl+C reaches the main thread instead of blocking on one future.
            _done, pending = wait(pending, timeout=1.0)
        for item, future in futures.items():
            results[item] = future.result()
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return results
