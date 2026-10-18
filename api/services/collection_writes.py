"""Serialize collection mutations within one API process.

The registry counts both owners and waiting writers. A guard spans synchronous
worker work; callers must enter it in their executor, never across an async wait.
"""
from contextlib import contextmanager
from functools import wraps
from inspect import signature
import threading

_registry_lock = threading.Lock()
_registry = {}


def canonical(collection):
    """The same first-character alias normalization used by the backend SDK."""
    return collection[:1].upper() + collection[1:]


def aliases(collection):
    """Canonical name and its accepted lowercase-first spelling, once each."""
    name = canonical(collection)
    return tuple(dict.fromkeys((name, name[:1].lower() + name[1:])))


class CollectionBusyError(TimeoutError):
    """A bounded caller could not acquire the collection mutation guard."""


@contextmanager
def guard(collection, timeout=None):
    collection = canonical(collection)
    with _registry_lock:
        entry = _registry.setdefault(collection, [threading.RLock(), 0])
        entry[1] += 1
    acquired = False
    try:
        acquired = entry[0].acquire() if timeout is None else entry[0].acquire(timeout=timeout)
        if not acquired:
            raise CollectionBusyError(collection)
        yield
    finally:
        if acquired:
            entry[0].release()
        with _registry_lock:
            entry[1] -= 1
            if not entry[1]:
                del _registry[collection]


def serialized(argument):
    def decorate(function):
        parameters = signature(function)
        @wraps(function)
        def run(*args, **kwargs):
            collection = parameters.bind(*args, **kwargs).arguments[argument]
            with guard(collection):
                return function(*args, **kwargs)
        return run
    return decorate


# ── Jobs active for a collection (issue #40) ──────────────────────────────────
# The guard above only serializes writers: a user's DELETE sent during an ingest
# waited for the whole job and then removed what it stored, and one that won the
# guard before a queued job let that job's inserts recreate the collection. Every
# long job (ingest, export, import, tune, gold-standard generation) registers
# here, under one lock and the canonical name, from admission until just before
# its status turns terminal, so the delete route can refuse instead of waiting.

_jobs_lock = threading.Lock()
_active_jobs = {}


def register_job(collection, kind, job_id):
    with _jobs_lock:
        _active_jobs.setdefault(canonical(collection), []).append((kind, job_id))


def end_job(kind, job_id):
    """End a registration; safe to repeat, and to call for one never made."""
    with _jobs_lock:
        for collection, entries in list(_active_jobs.items()):
            if (kind, job_id) in entries:
                entries.remove((kind, job_id))
                if not entries:
                    del _active_jobs[collection]
                return


def active_jobs(collection):
    """The jobs registered for a collection, oldest first."""
    with _jobs_lock:
        return list(_active_jobs.get(canonical(collection), ()))


class CollectionInUseError(RuntimeError):
    """A delete was refused because jobs are registered for the collection."""

    def __init__(self, collection, jobs):
        super().__init__(collection)
        self.collection = collection
        self.jobs = jobs


def delete_when_idle(collection, delete, poll=0.25):
    """Run delete(collection) under its guard unless a job is registered.

    The guard is awaited only while no job is registered, rechecked every `poll`
    seconds, so a job admitted while the delete waits still refuses it promptly.
    A job admitted after the last check is not refused: its worker waits for this
    guard and then meets a deleted collection, which cannot be recreated
    (AUTOSCHEMA_ENABLED is off) and which ingest reports as deleted.
    """
    def refuse_if_busy():
        jobs = active_jobs(collection)
        if jobs:
            raise CollectionInUseError(collection, jobs)

    refuse_if_busy()
    while True:
        try:
            with guard(collection, timeout=poll):
                refuse_if_busy()
                return delete(collection)
        except CollectionBusyError:
            refuse_if_busy()
