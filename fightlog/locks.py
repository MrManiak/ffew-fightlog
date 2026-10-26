"""Cross-process locks on the Django cache.

Uses django-redis' native lock when available, and an add()-based lock otherwise
(the local-memory cache in tests).
"""

import time
import uuid
from contextlib import contextmanager

from django.core.cache import cache


class LockBusy(RuntimeError):
    pass


@contextmanager
def cache_lock(name: str, timeout: int = 600, blocking: bool = True, wait: float = 30.0):
    key = f"fightlog-lock-{name}"
    native = getattr(cache, "lock", None)
    if native is not None:
        lock = native(key, timeout=timeout)
        acquired = lock.acquire(blocking=blocking, blocking_timeout=wait if blocking else None)
        if not acquired:
            raise LockBusy(name)
        try:
            yield
        finally:
            try:
                lock.release()
            except Exception:  # pragma: no cover - expired lock
                pass
        return
    token = uuid.uuid4().hex
    deadline = time.monotonic() + wait
    while not cache.add(key, token, timeout):
        if not blocking or time.monotonic() > deadline:
            raise LockBusy(name)
        time.sleep(0.05)
    try:
        yield
    finally:
        if cache.get(key) == token:
            cache.delete(key)
