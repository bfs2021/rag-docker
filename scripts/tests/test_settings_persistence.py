"""Deterministic settings publication regressions; no backend or model calls."""
import errno
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from services import ingest_config, retrieval_config, settings_store

SERVICES = (ingest_config, retrieval_config)


class SettingsPersistenceTests(unittest.TestCase):
    @contextmanager
    def fixture(self, service):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(settings, 'upload_dir', directory), \
             patch.object(service, '_DIR', None):
            yield Path(directory)

    def values(self, service):
        field = 'chunk_size' if service is ingest_config else 'top_k'
        return [{**service.DEFAULTS, 'collection': 'ConcurrentSettings', field: value}
                for value in ((1000, 1100, 1200) if service is ingest_config else (5, 7, 9))]

    def test_import_publication_preserves_config_on_failure_and_uses_shared_lock(self):
        from services import importer
        with self.fixture(ingest_config) as root:
            old, incoming, _ = self.values(ingest_config)
            ingest_config.save(old)
            pkg = root / 'package'
            pkg.mkdir()
            (pkg / 'ingest_config.json').write_text(json.dumps(incoming))
            path = ingest_config._path(old['collection'])
            before = path.read_bytes()
            entered = []
            class ObservedLock:
                def __enter__(self):
                    entered.append(True)
                def __exit__(self, *args):
                    pass
            with patch.object(settings_store, '_write_lock', ObservedLock()), \
                 patch.object(Path, 'replace', side_effect=OSError('owned publication failure')):
                with self.assertRaises(OSError):
                    importer._restore_sidecars(old['collection'], pkg, 'Original', [])
            self.assertEqual(entered, [True])
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob('*.tmp')), [])
            importer._restore_sidecars(old['collection'], pkg, 'Original', [])
            self.assertEqual(ingest_config.load(old['collection']), incoming)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_contending_saves_publish_their_own_complete_values(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__), self.fixture(service):
                old, first, second = self.values(service)
                service.save(old)
                reached_replace = threading.Event()
                second_attempt = threading.Event()
                release_first = threading.Event()
                lock = threading.Lock()
                request = threading.local()
                publications = []
                replace = Path.replace

                class ObservedLock:
                    def __enter__(self):
                        if request.value == second:
                            second_attempt.set()
                        lock.acquire()

                    def __exit__(self, *args):
                        lock.release()

                def controlled_replace(temporary, destination):
                    if request.value == first:
                        reached_replace.set()
                        if not release_first.wait(5):
                            raise TimeoutError('first publication was not released')
                    payload = json.loads(temporary.read_text())
                    self.assertEqual(payload, request.value)
                    result = replace(temporary, destination)
                    self.assertEqual(service.load(old['collection']), request.value)
                    publications.append((temporary, payload))
                    return result

                def save(value):
                    request.value = value
                    return service.save(value)

                with patch.object(settings_store, '_write_lock', ObservedLock()), \
                     patch.object(Path, 'replace', controlled_replace), \
                     ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(save, first)
                    try:
                        self.assertTrue(reached_replace.wait(5))
                        b = pool.submit(save, second)
                        self.assertTrue(second_attempt.wait(5))
                        self.assertFalse(b.done())
                        self.assertEqual(service.load(old['collection']), old)
                        self.assertEqual(len(list(service._dir().glob('*.tmp'))), 1)
                    finally:
                        release_first.set()
                    self.assertEqual(a.result(timeout=5), first)
                    self.assertEqual(b.result(timeout=5), second)
                self.assertEqual([value for _, value in publications], [first, second])
                self.assertEqual(len({path for path, _ in publications}), 2)
                self.assertTrue(all(path.parent == service._dir() for path, _ in publications))
                self.assertEqual(service.resolve(old['collection']), (second, False))
                self.assertEqual(list(service._dir().glob('*.tmp')), [])

    def test_first_saves_wait_for_their_directory_to_exist(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__), self.fixture(service):
                _, first, second = self.values(service)
                reached_mkdir = threading.Event()
                release_first = threading.Event()
                mkdir = Path.mkdir
                request = threading.local()

                def controlled_mkdir(path, *args, **kwargs):
                    if request.value == first:
                        reached_mkdir.set()
                        if not release_first.wait(5):
                            raise TimeoutError('directory creation was not released')
                    return mkdir(path, *args, **kwargs)

                def save(value):
                    request.value = value
                    return service.save(value)

                with patch.object(Path, 'mkdir', controlled_mkdir), ThreadPoolExecutor(max_workers=2) as pool:
                    a = pool.submit(save, first)
                    try:
                        self.assertTrue(reached_mkdir.wait(5))
                        b = pool.submit(save, second)
                        self.assertEqual(b.result(timeout=5), second)
                        self.assertEqual(service.load(first['collection']), second)
                    finally:
                        release_first.set()
                    self.assertEqual(a.result(timeout=5), first)
                self.assertEqual(service.load(first['collection']), first)

    def assert_failure_preserves_old_value(self, service, fault, exception):
        with self.fixture(service):
            old, attempted, _ = self.values(service)
            service.save(old)
            previous_bytes = service._path(old['collection']).read_bytes()
            with fault, self.assertRaises(exception):
                service.save(attempted)
            self.assertEqual(service._path(old['collection']).read_bytes(), previous_bytes)
            self.assertEqual(service.resolve(old['collection']), (old, False))
            self.assertEqual(list(service._dir().glob('*.tmp')), [])
            self.assertEqual(service.save(attempted), attempted)
            self.assertEqual(service.load(old['collection']), attempted)

    def test_temporary_creation_failure_preserves_old_value_and_allows_retry(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__):
                self.assert_failure_preserves_old_value(service,
                    patch.object(settings_store.tempfile, 'NamedTemporaryFile', side_effect=PermissionError('injected create fault')),
                    PermissionError)

    def test_partial_write_and_close_failure_preserve_old_value_and_clean_up(self):
        create = tempfile.NamedTemporaryFile
        for service in SERVICES:
            for stage in ('write', 'close'):
                with self.subTest(service=service.__name__, stage=stage):
                    @contextmanager
                    def failing_file(*args, **kwargs):
                        with create(*args, **kwargs) as output:
                            if stage == 'write':
                                def partial_write(payload):
                                    output.file.write(payload[:10])
                                    raise OSError('injected partial write fault')
                                with patch.object(output, 'write', partial_write):
                                    yield output
                            else:
                                yield output
                                raise OSError('injected close fault')
                    self.assert_failure_preserves_old_value(service,
                        patch.object(settings_store.tempfile, 'NamedTemporaryFile', failing_file), OSError)

    def test_replacement_failure_preserves_old_value_and_cleans_up(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__):
                self.assert_failure_preserves_old_value(service,
                    patch.object(Path, 'replace', side_effect=OSError('injected replace fault')), OSError)

    @contextmanager
    def recorded(self, events, fail_folder_sync=False):
        """Record replace and the folder sync's open, fsync and close (#445).
        With fail_folder_sync, an fsync of a folder fails as on a broken volume."""
        real_replace, real_open = Path.replace, os.open
        real_fsync, real_close = os.fsync, os.close

        def replace(source, target):
            events.append(('replace', Path(target)))
            return real_replace(source, target)

        def open_(path, flags, *args):
            fd = real_open(path, flags, *args)
            events.append(('open', fd, Path(path)))
            return fd

        def fsync(fd):
            folder = stat.S_ISDIR(os.fstat(fd).st_mode)
            events.append(('fsync folder' if folder else 'fsync file', fd))
            if folder and fail_folder_sync:
                raise OSError(errno.EIO, 'injected folder sync fault')
            return real_fsync(fd)

        def close(fd):
            events.append(('close', fd))
            return real_close(fd)

        with patch.object(Path, 'replace', replace), \
             patch.object(os, 'open', side_effect=open_), \
             patch.object(os, 'fsync', side_effect=fsync), \
             patch.object(os, 'close', side_effect=close):
            yield

    def after_replace(self, events):
        names = [event[0] for event in events]
        self.assertEqual(names.count('replace'), 1, events)
        return events[names.index('replace') + 1:]

    def test_the_folder_is_fsynced_after_the_replacement(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__), self.fixture(service):
                old, attempted, _ = self.values(service)
                service.save(old)
                events = []
                with self.recorded(events):
                    service.save(attempted)
                after = self.after_replace(events)
                self.assertEqual([event[0] for event in after], ['open', 'fsync folder', 'close'], events)
                fd, folder = after[0][1], after[0][2]
                self.assertEqual(folder, service._dir())
                self.assertEqual((after[1][1], after[2][1]), (fd, fd))
                self.assertEqual(service.load(old['collection']), attempted)
                self.assertEqual(list(service._dir().glob('*.tmp')), [])

    def test_import_publication_fsyncs_the_folder_after_the_replacement(self):
        from services import importer
        with self.fixture(ingest_config) as root:
            _, incoming, _ = self.values(ingest_config)
            pkg = root / 'package'
            pkg.mkdir()
            (pkg / 'ingest_config.json').write_text(json.dumps(incoming))
            events = []
            with self.recorded(events):
                importer._restore_sidecars(incoming['collection'], pkg, 'Original', [])
            after = self.after_replace(events)
            self.assertEqual([event[0] for event in after], ['open', 'fsync folder', 'close'], events)
            self.assertEqual(after[0][2], ingest_config._path(incoming['collection']).parent)
            self.assertEqual(ingest_config.load(incoming['collection']), incoming)

    def test_a_failed_folder_sync_raises_with_the_new_value_in_place(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__), self.fixture(service):
                old, attempted, _ = self.values(service)
                service.save(old)
                events = []
                with self.recorded(events, fail_folder_sync=True), self.assertRaises(OSError):
                    service.save(attempted)
                after = self.after_replace(events)
                self.assertEqual([event[0] for event in after], ['open', 'fsync folder', 'close'], events)
                self.assertEqual(after[2][1], after[0][1], "the folder's descriptor was left open")
                self.assertEqual(service.load(old['collection']), attempted)
                self.assertEqual(list(service._dir().glob('*.tmp')), [])

    def test_serialization_failure_preserves_old_value(self):
        for service in SERVICES:
            with self.subTest(service=service.__name__), self.fixture(service):
                old, _, _ = self.values(service)
                service.save(old)
                with self.assertRaises(TypeError):
                    service.save({**old, 'unserializable': object()})
                self.assertEqual(service.load(old['collection']), old)
                self.assertEqual(list(service._dir().glob('*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
