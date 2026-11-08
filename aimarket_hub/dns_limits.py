"""Run synchronous DNS/URL guards off-loop with bounded, non-queueing capacity.

Only pure resolver/validation functions belong here, never database writes or
HTTP dispatch. A caller timing out cannot cancel libc DNS, so its daemon worker
keeps its slot until that resolver actually returns.
"""
from __future__ import annotations

import asyncio
import math
import threading

MAX_DNS_WORKERS = 4
DEFAULT_DNS_TIMEOUT_S = 5.0
_slots = threading.BoundedSemaphore(MAX_DNS_WORKERS)


class DNSCheckBusy(ValueError):
    """All resolver workers are occupied; no work was queued."""


class DNSCheckTimeout(TimeoutError):
    """The caller's resolver deadline elapsed; its worker remains bounded."""


async def bounded_dns_call(function, *args, timeout_s=None, **kwargs):
    """Return a pure synchronous guard's result, or fail closed without a queue."""
    timeout = float(DEFAULT_DNS_TIMEOUT_S if timeout_s is None else timeout_s)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("DNS guard timeout must be positive and finite")
    slots = _slots
    if not slots.acquire(blocking=False):
        raise DNSCheckBusy("DNS/URL validation capacity exhausted")
    loop = asyncio.get_running_loop()
    result = loop.create_future()

    def worker():
        value = None
        error = None
        try:
            value = function(*args, **kwargs)
        except Exception as exc:
            error = exc
        except BaseException:
            error = RuntimeError("DNS/URL guard failed")
        finally:
            # A timed-out/cancelled coroutine does not release a still-blocked
            # resolver's slot, and cannot create unlimited replacement threads.
            slots.release()

        def deliver():
            if result.done():
                return
            if error is not None:
                result.set_exception(error)
            else:
                result.set_result(value)

        try:
            loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            # A daemon worker may finish after its caller's event loop closed.
            pass

    thread = threading.Thread(target=worker, name="aimarket-dns-guard", daemon=True)
    try:
        thread.start()
    except BaseException:
        slots.release()
        raise
    try:
        return await asyncio.wait_for(result, timeout=timeout)
    except TimeoutError as exc:
        raise DNSCheckTimeout("DNS/URL validation deadline exceeded") from exc
