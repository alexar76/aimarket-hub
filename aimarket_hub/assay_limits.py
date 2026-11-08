"""Bound work started by unauthenticated federation announcements."""
from __future__ import annotations

import asyncio
import functools
import logging
import weakref

MAX_CONCURRENT_ASSAYS = 4
_limits = weakref.WeakKeyDictionary()
_background_tasks = weakref.WeakKeyDictionary()
logger = logging.getLogger(__name__)


def schedule_background_assay(url, coroutine_factory) -> bool:
    """Schedule at most four knock tasks per loop, with no duplicate or waiting queue.

    The factory is called only inside an accepted task, so a refused knock cannot
    create an unawaited coroutine. Strong task references last until completion;
    cancellation and failure release the same URL/capacity as a successful assay.
    Admin and periodic calls retain the separate active-work semaphore below.
    """
    key = str(url or '').strip().rstrip('/')
    if not key:
        return False
    loop = asyncio.get_running_loop()
    tasks = _background_tasks.get(loop)
    if tasks is None:
        tasks = {}
        _background_tasks[loop] = tasks
    # A task can have completed before its done callback has received a loop turn.
    for finished_key, task in list(tasks.items()):
        if task.done():
            tasks.pop(finished_key, None)
    if key in tasks or len(tasks) >= MAX_CONCURRENT_ASSAYS:
        return False

    async def run():
        return await coroutine_factory()

    coroutine = run()
    try:
        task = loop.create_task(coroutine, name='federation-knock-assay')
    except RuntimeError:
        coroutine.close()
        if not tasks:
            _background_tasks.pop(loop, None)
        return False
    tasks[key] = task

    def completed(done):
        registry = _background_tasks.get(loop)
        if registry is not None:
            if registry.get(key) is done:
                registry.pop(key, None)
            if not registry:
                _background_tasks.pop(loop, None)
        if not done.cancelled():
            error = done.exception()
            if error is not None:
                logger.warning('background federation assay failed',
                               exc_info=(type(error), error, error.__traceback__))

    task.add_done_callback(completed)
    return True


def limit_assay_concurrency(function):
    """Limit entire assays, including DNS, HTTP response buffers and judge calls.

    A separate semaphore belongs to each event loop. Only active/queued calls
    keep it alive, so test clients or restarted loops cannot leak loop objects.
    """
    @functools.wraps(function)
    async def limited(*args, **kwargs):
        loop = asyncio.get_running_loop()
        reference = _limits.get(loop)
        semaphore = reference() if reference is not None else None
        if semaphore is None:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_ASSAYS)
            _limits[loop] = weakref.ref(semaphore)
        async with semaphore:
            return await function(*args, **kwargs)
    return limited
