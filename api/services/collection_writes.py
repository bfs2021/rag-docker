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
# Collections a delete is removing right now, canonical name -> count (#302).
# A delete enters it under _jobs_lock in the same step as its last busy check,
# so a job either registers first and refuses the delete, or is refused here.
_deleting = {}


class CollectionDeletingError(Exception):
    """A job was refused because a delete of its collection is in progress.

    Not a RuntimeError: the export and tune routes read RuntimeError as "already
    running", which this is not.
    """

    def __init__(self, collection):
        super().__init__(collection)
        self.collection = collection


def deleting_message(name):
    return (f"Collection '{name}' is being deleted. Start the job again once the delete "
            "finishes; the collection must be created again first.")


def register_job(collection, kind, job_id):
    """Register a job; raises CollectionDeletingError while a delete is in progress."""
    with _jobs_lock:
        if canonical(collection) in _deleting:
            raise CollectionDeletingError(collection)
        _active_jobs.setdefault(canonical(collection), []).append((kind, job_id))


def delete_in_progress(collection):
    with _jobs_lock:
        return canonical(collection) in _deleting


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
    Once the delete holds the guard and its last check finds no job, the delete
    is in progress until it returns: a job registering then is refused with
    CollectionDeletingError (#302) instead of meeting a deleted collection.
    """
    def refuse_if_busy():
        jobs = active_jobs(collection)
        if jobs:
            raise CollectionInUseError(collection, jobs)

    name = canonical(collection)
    refuse_if_busy()
    while True:
        try:
            with guard(collection, timeout=poll):
                with _jobs_lock:
                    jobs = list(_active_jobs.get(name, ()))
                    if jobs:
                        raise CollectionInUseError(collection, jobs)
                    _deleting[name] = _deleting.get(name, 0) + 1
                try:
                    return delete(collection)
                finally:
                    with _jobs_lock:
                        _deleting[name] -= 1
                        if not _deleting[name]:
                            del _deleting[name]
        except CollectionBusyError:
            refuse_if_busy()
