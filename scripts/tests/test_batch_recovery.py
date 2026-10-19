"""Controlled final-flush faults against real writer and recovery services."""
import asyncio
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
import uuid
import weakref
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from services import batch_write, collection_recovery as recovery, importer, tuning, weaviate_client as wc


def records(n=2):
    return [dict(id=str(uuid.uuid4()), vector=[0.25, 0.5], properties={
        'content': f'chunk {i}', 'created_at': '2026-09-27T00:00:00Z'}) for i in range(n)]


class Batch:
    def __init__(self, collection):
        self.collection = collection
        self.failed_objects = []
        self.number_errors = 0

    def dynamic(self):
        self.failed_objects = []
        self.pending = []
        return self

    def __enter__(self):
        return self

    def add_object(self, properties, uuid, vector=None):
        self.pending.append(dict(id=str(uuid), properties=copy.deepcopy(properties),
                                 vector=copy.deepcopy(vector) if vector is not None else [0.25, 0.5]))

    def __exit__(self, *args):
        fault = self.collection.fault
        if fault == 'reject':
            self.failed_objects = [object()]
            return
        pending = self.pending[:-1] if fault == 'partial' else self.pending
        for record in pending:
            if fault == 'identities':
                record['id'] = str(uuid.uuid4())
            if fault == 'properties':
                record['properties']['content'] = 'wrong'
            if fault == 'invalid_vector':
                record['vector'] = []
            if fault == 'vector':
                record['vector'] = [9.0, 9.0]
            self.collection.rows[record['id']] = record
        if fault == 'extra':
            extra = records(1)[0]
            self.collection.rows[extra['id']] = extra


class Collection:
    def __init__(self, name, fault=None, description=None):
        self.name = name
        self.rows = {}
        self.fault = fault
        self.description = description
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(name=self.name, description=self.description, vectorizer=None))
        self.batch = Batch(self)
        self.query = SimpleNamespace(fetch_objects=lambda filters, **kwargs: SimpleNamespace(
            objects=[obj for obj in self.iterator(include_vector=True) if str(obj.uuid) in filters.value]))
        self.data = SimpleNamespace(delete_many=self.delete_many)
        self.aggregate = SimpleNamespace(over_all=lambda **kwargs: SimpleNamespace(total_count=len(self.rows)))

    def delete_many(self, where):
        for key in where.value:
            self.rows.pop(key, None)
        return SimpleNamespace(failed=0)

    def iterator(self, include_vector=False):
        if self.fault == 'read':
            raise OSError('backend unavailable')
        for row in self.rows.values():
            props = {'source_file': None, 'chunk_size': None, **copy.deepcopy(row['properties'])}
            if isinstance(props.get('created_at'), str):
                props['created_at'] = datetime.fromisoformat(props['created_at'].replace('Z', '+00:00'))
            yield SimpleNamespace(uuid=uuid.UUID(row['id']), properties=props,
                                  vector={'default': row['vector']})


class Collections:
    def __init__(self):
        self.items = {}
        self.deleted = []
        self.final_fault = None
        self.staging_fault = None
        self.create_failure = False
        self.interrupt = False

    def exists(self, name):
        return name in self.items

    def get(self, name):
        return self.items[name]

    def create(self, name, *args, description=None):
        if name == 'Corpus' and self.create_failure:
            raise RuntimeError('final create failed')
        fault = self.final_fault if name == 'Corpus' else self.staging_fault
        self.items[name] = Collection(name, fault, description)

    def delete(self, name):
        self.deleted.append(name)
        self.items.pop(name, None)
        if name == 'Corpus' and self.interrupt:
            raise KeyboardInterrupt('hard-kill boundary')


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        setting = patch.object(settings, 'upload_dir', self.temp.name)
        setting.start()
        self.addCleanup(setting.stop)

    def test_final_flush_rejection_is_observed_after_exit(self):
        col = Collection('Corpus', 'reject')
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            batch_write.insert(col, records())
        self.assertEqual(col.batch.number_errors, 0)

    def test_same_count_wrong_records_and_partial_writes_fail(self):
        for fault in ('partial', 'identities', 'properties', 'vector', 'extra'):
            with self.subTest(fault=fault), self.assertRaises(batch_write.BatchVerificationError):
                batch_write.insert(Collection('Corpus', fault), records())

    def test_duplicate_and_declared_count_fail_before_enqueue(self):
        row = records(1)[0]
        for rows, expected in (([row, row], 2), ([row], 2)):
            col = Collection('Corpus')
            with self.assertRaises(ValueError):
                batch_write.insert(col, rows, expected_count=expected)
            self.assertFalse(col.rows)
            self.assertFalse(hasattr(col.batch, 'pending'))

    def test_empty_vector_and_nonfinite_vector_fail_before_enqueue(self):
        for vector in ([], [float('nan')], [float('inf')], ['0.1']):
            col = Collection('Corpus')
            with self.assertRaises(ValueError):
                batch_write.insert(col, [{**records(1)[0], 'vector': vector}])
            self.assertFalse(col.rows)

    def test_additions_preserve_existing_objects_and_confirm_only_new_records(self):
        col = Collection('Corpus')
        batch_write.insert(col, records(1))
        batch_write.insert(col, [{'properties': {'content': 'new'}}], exact=False)
        self.assertEqual(len(col.rows), 2)

    def test_vectorless_storage_is_accepted_only_for_vectorizer_none(self):
        class VectorlessBatch(Batch):
            def add_object(self, properties, uuid, vector=None):
                self.pending.append(dict(id=str(uuid), properties=copy.deepcopy(properties),
                                         vector=None))

        col = Collection('TextOnly')
        col.batch = VectorlessBatch(col)
        row = [{'properties': {'content': 'inert text'}}]
        with self.assertRaises(batch_write.BatchVerificationError):
            batch_write.insert(col, row, exact=False)
        col.config = SimpleNamespace(get=lambda: SimpleNamespace(
            vectorizer=SimpleNamespace(value='none')))
        self.assertEqual(batch_write.insert(col, row, exact=False), 1)

    def test_ingestion_uses_completed_batch_verification(self):
        col = Collection('Corpus', 'reject')
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: col))
        with patch.object(wc, 'get_client', return_value=client), self.assertRaises(RuntimeError):
            wc._insert_chunks_sync('Corpus', [{'content': 'new'}])

    def test_generated_ingestion_failure_rolls_back_only_attempt_ids(self):
        for fault in ('partial', 'properties', 'invalid_vector', 'reject'):
            with self.subTest(fault=fault):
                col = Collection('Corpus')
                previous = records(1)
                batch_write.insert(col, previous)
                col.fault = fault
                client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: col))
                with patch.object(wc, 'get_client', return_value=client), self.assertRaises(RuntimeError):
                    wc._insert_chunks_sync('Corpus', [{'content': 'new 1'}, {'content': 'new 2'}])
                self.assertEqual(set(col.rows), {previous[0]['id']})

    def test_ingestion_cleanup_failure_preserves_original_error_and_reports_uncertainty(self):
        col = Collection('Corpus', 'partial')
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: col))
        with patch.object(wc, 'get_client', return_value=client), \
                patch.object(col.data, 'delete_many', side_effect=OSError('cleanup outage')), \
                self.assertRaises(batch_write.BatchCleanupError) as error:
            wc._insert_chunks_sync('Corpus', [{'content': 'new 1'}, {'content': 'new 2'}])
        self.assertIsInstance(error.exception.original, batch_write.BatchVerificationError)
        self.assertIn('Accepted chunks may remain', str(error.exception))
        self.assertEqual(len(col.rows), 1)

    def test_reusable_stream_keeps_decoded_record_lifetime_bounded(self):
        live = weakref.WeakSet()
        class Record(dict):
            __hash__ = object.__hash__
        def stream():
            for i in range(500):
                record = Record(id=str(uuid.uuid5(uuid.NAMESPACE_OID, str(i))),
                                properties={'content': 'synthetic ' * 100}, vector=[0.25] * 32)
                live.add(record)
                self.assertLessEqual(len(live), 2)
                yield record
        col = Collection('Corpus')
        self.assertEqual(batch_write.insert(col, stream, expected_count=500), 500)

    def test_collection_creation_records_an_optional_description(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        with patch.object(wc, 'get_client', return_value=client):
            wc._create_collection_sync('Owned', 'hnsw', 'cosine', {}, description='rag-import:' + 'a' * 32)
            wc._create_collection_sync('Plain', 'hnsw', 'cosine', {})
        first, second = client.collections.create.call_args_list
        self.assertEqual(first.kwargs['description'], 'rag-import:' + 'a' * 32)
        self.assertIsNone(second.kwargs['description'])



class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key in ('upload_dir', 'sources_dir', 'exports_dir'):
            path = self.root / key
            path.mkdir()
            self.stack.enter_context(patch.object(settings, key, str(path)))
        self.cols = Collections()
        self.cols.create('Corpus')
        self.original = records()
        batch_write.insert(self.cols.get('Corpus'), self.original)
        self.client = SimpleNamespace(collections=self.cols)
        self.stack.enter_context(patch.object(wc, 'get_client', return_value=self.client))
        self.stack.enter_context(patch.object(wc, '_create_collection_sync', side_effect=lambda name, *args, **kwargs: self.cols.create(name, description=kwargs.get('description'))))
        self.stack.enter_context(patch.object(wc, '_collection_config_sync', return_value={'index_type': 'hnsw', 'distance_metric': 'cosine'}))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.dict(tuning._jobs, {}, clear=True))
        self.stack.enter_context(patch.object(wc.ingest_config, '_DIR', None))
        self.stack.enter_context(patch.object(wc.retrieval_config, '_DIR', None))
        self.stack.enter_context(patch.object(importer, '_log'))
        self.stack.enter_context(patch.object(recovery, 'log'))
        self.stack.enter_context(patch.object(importer.goldstandard, 'sessions_for', return_value=[]))
        self.stack.enter_context(patch.object(importer.goldstandard, 'mark_orphaned', return_value=0))
        source = Path(settings.sources_dir) / 'Corpus'
        source.mkdir()
        (source / 'source-blob').write_bytes(b'original source')
        for kind in ('ingest', 'retrieval'):
            directory = Path(settings.upload_dir) / f'{kind}_configs'
            directory.mkdir()
            (directory / 'Corpus.json').write_text(json.dumps({'collection': 'Corpus', 'setting': kind}))
        directory = Path(settings.upload_dir) / 'goldstandard_sessions'
        directory.mkdir()
        (directory / 'gs_0123abcd.json').write_text(json.dumps({'collection': 'Corpus', 'pairs': ['preserve']}))

    def recovery_record(self):
        paths = list(recovery._root().glob('*.json'))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual(record['state'], 'recovery')
        return record

    def assert_retained(self, record):
        name = record['staging']
        before = copy.deepcopy(self.cols.get(name).rows)
        recovery.sweep(self.client)
        self.assertEqual(self.cols.get(name).rows, before)
        blob = hashlib.sha256(b'original source').hexdigest() if record['operation'] == 'import' else 'source-blob'
        self.assertEqual((Path(settings.sources_dir) / name / blob).read_bytes(), b'original source')
        metadata = recovery._root() / record['operation_id']
        self.assertTrue((metadata / 'goldstandard' / 'gs_0123abcd.json').is_file())
        for kind in ('ingest', 'retrieval'):
            self.assertEqual(json.loads((Path(settings.upload_dir) / f'{kind}_configs' / f'{name}.json').read_text())['collection'], name)

    def rebuild(self):
        return tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None)

    def test_tuning_final_create_failure_retains_verified_data_and_sidecars(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        record = self.recovery_record()
        self.assertEqual(error.exception.detail['recovered_as'], record['staging'])
        self.assert_retained(record)

    def test_tuning_final_batch_and_verification_failures_retain_data(self):
        for fault in ('reject', 'partial', 'properties', 'vector'):
            with self.subTest(fault=fault):
                self.cols.final_fault = fault
                self.cols.items['Corpus'] = Collection('Corpus')
                with self.assertRaises(importer.PackageError):
                    self.rebuild()
                paths = list(recovery._root().glob('*.json'))
                record = json.loads(paths[-1].read_text())
                self.assert_retained(record)
                for p in paths:
                    recovery.discard(json.loads(p.read_text()), self.client)

    def test_restart_after_destructive_boundary_preserves_recovery(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        self.assertFalse(self.cols.exists('Corpus'))
        self.assert_retained(self.recovery_record())

    def test_scratch_and_markerlike_unowned_names_are_distinguished(self):
        owned = recovery.begin('Corpus', 'tune', self.client)
        self.cols.create(owned['staging'])
        unowned = 'User__tuning_dataset'
        self.cols.create(unowned)
        old = 'Corpus__importing_1234abcd'
        self.cols.create(old)
        self.assertEqual(recovery.sweep(self.client), [owned['staging']])
        self.assertTrue(self.cols.exists(unowned))
        self.assertTrue(self.cols.exists(old))

    def test_corrupt_ownership_does_not_authorize_deletion(self):
        record = recovery.begin('Corpus', 'tune', self.client)
        self.cols.create(record['staging'])
        path = recovery._root() / (record['operation_id'] + '.json')
        path.write_text('{')
        self.assertEqual(recovery.sweep(self.client), [])
        self.assertTrue(path.exists())
        self.assertTrue(self.cols.exists(record['staging']))

    def test_snapshot_failure_preserves_original_and_cleans_owned_scratch(self):
        with patch.object(recovery, '_copy', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.rebuild()
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))

    def test_failure_while_publishing_confirmation_preserves_named_recovery(self):
        def fail_progress(count):
            raise RuntimeError('confirmation callback failed')
        with self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, fail_progress)
        record = self.recovery_record()
        self.assertEqual(error.exception.detail['recovered_as'], record['staging'])
        self.assert_retained(record)

    def test_success_reports_only_confirmed_target_and_removes_recovery(self):
        progress = []
        written = tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, progress.append)
        self.assertEqual(written, 2)
        self.assertEqual(progress, [2])
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))

    def package(self):
        pkg = self.root / 'package'
        pkg.mkdir(exist_ok=True)
        (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.original))
        source = pkg / 'sources'
        source.mkdir(exist_ok=True)
        digest = hashlib.sha256(b'original source').hexdigest()
        (source / digest).write_bytes(b'original source')
        (source / 'index.json').write_text(json.dumps({'version': 1, 'documents': {digest: {'filenames': ['source.txt']}}}))
        for kind in ('ingest', 'retrieval'):
            (pkg / f'{kind}_config.json').write_text(json.dumps({'collection': 'Corpus', 'setting': kind}))
        gold = pkg / 'goldstandard'
        gold.mkdir(exist_ok=True)
        (gold / 'gs_0123abcd.json').write_text(json.dumps({
            'session_id': 'gs_0123abcd', 'collection': 'Corpus', 'status': 'completed',
            'pairs_total': 1, 'pairs_completed': 1,
            'pairs': [{'pair_id': 'p_0123abcd', 'question': 'Question?',
                       'answer': 'Answer', 'ground_truth': 'Answer',
                       'contexts': ['Context'], 'source_file': 'source.txt',
                       'chunk_index': 0, 'status': 'approved'}]}))
        manifest = {'package_format': 1, 'collection': {'name': 'Corpus', 'chunk_count': 2},
                    'embedding': {'dimensions': 2, 'model': settings.embed_model}}
        (pkg / 'manifest.json').write_text(json.dumps(manifest))
        (pkg / 'collection.json').write_text('{}')
        return pkg, manifest

    def import_replace(self):
        pkg, manifest = self.package()
        importer._jobs['job'] = dict(chunks_written=0)
        with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                patch.object(importer.packager, 'verify_digests'), \
                patch.object(importer, '_check_embedding'), \
                patch.object(importer, '_ensure_models', return_value=[]):
            importer._run('job', 'fixture.tar.gz', 'replace')
        return importer._jobs['job']

    def test_interrupted_tuning_partial_target_is_checked_at_startup(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.assertTrue(record['cutover_pending'])
        self.cols.interrupt = False
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(self.cols.exists(record['staging']))
        stale.assert_called_once()

    def test_interrupted_tuning_never_deletes_another_target_instance(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.cols.interrupt = False
        self.cols.create('Corpus', description='new user collection')
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
        self.assertTrue(self.cols.exists('Corpus'))
        stale.assert_not_called()

    def test_interrupted_tuning_keeps_complete_target_and_unreadable_state(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.cols.interrupt = False
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        self.cols.get('Corpus').rows = copy.deepcopy(self.cols.get(record['staging']).rows)
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            self.assertTrue(self.cols.exists('Corpus'))
            stale.assert_not_called()
            self.cols.get('Corpus').fault = 'read'
            recovery.sweep(self.client)
            self.assertTrue(self.cols.exists('Corpus'))
            stale.assert_not_called()

    def interrupted_cutover(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        self.cols.interrupt = False
        record = self.recovery_record()
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        return record

    def test_startup_check_flags_an_interrupted_cutover_only_once(self):
        record = self.interrupted_cutover()
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
        stale.assert_called_once_with('Corpus', 'interrupted tuning final write; verified recovery retained')
        checked = self.recovery_record()
        self.assertFalse(checked['cutover_pending'])
        self.assertEqual(checked['cutover_checked'], 'stale')
        self.assertTrue(self.cols.exists(record['staging']))
        self.assertTrue(self.cols.exists('Corpus'))

    def test_complete_cutover_is_not_rechecked_after_later_mutation(self):
        record = self.interrupted_cutover()
        self.cols.get('Corpus').rows = copy.deepcopy(self.cols.get(record['staging']).rows)
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            self.assertEqual(self.recovery_record()['cutover_checked'], 'complete')
            self.cols.get('Corpus').rows.clear()
            with patch.object(batch_write, 'verify', side_effect=AssertionError('rechecked')) as verify:
                recovery.sweep(self.client)
                verify.assert_not_called()
            stale.assert_not_called()

    def test_other_instance_decision_is_retired_without_touching_sessions(self):
        record = self.interrupted_cutover()
        self.cols.get('Corpus').config = SimpleNamespace(get=lambda: SimpleNamespace(description='later'))
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
            stale.assert_not_called()
        self.assertEqual(self.recovery_record()['cutover_checked'], 'other-instance')
        self.assertTrue(self.cols.exists(record['staging']))

    def test_unreadable_cutover_stays_pending_until_check_can_finish(self):
        record = self.interrupted_cutover()
        self.cols.get('Corpus').fault = 'read'
        recovery.sweep(self.client)
        self.assertTrue(self.recovery_record()['cutover_pending'])
        self.cols.get('Corpus').fault = None
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
            stale.assert_called_once()
        self.assertFalse(self.recovery_record()['cutover_pending'])

    def test_failed_outcome_write_keeps_cutover_pending_for_retry(self):
        self.interrupted_cutover()
        with patch.object(tuning.goldstandard, 'mark_stale'), patch.object(recovery, '_write', side_effect=OSError('disk full')):
            recovery.sweep(self.client)
        self.assertTrue(self.recovery_record()['cutover_pending'])
        with patch.object(tuning.goldstandard, 'mark_stale'):
            recovery.sweep(self.client)
        self.assertFalse(self.recovery_record()['cutover_pending'])

    def test_import_replace_note_counts_canonical_and_alias_sessions(self):
        seen = []
        def sessions(name):
            seen.append(name)
            return [{'session_id': 'gs_18000001'}] if name == 'Corpus' else [{'session_id': 'gs_18000002'}]
        with patch.object(importer.goldstandard, 'sessions_for', side_effect=sessions):
            job = self.import_replace()
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(set(seen), {'Corpus', 'corpus'})
        self.assertTrue(any('2 gold-standard session(s)' in note for note in job['notes']), job)

    def assert_replace_note_count(self, by_spelling, expected):
        # #180: the note counts retained sessions across both spellings, each session once.
        with patch.object(importer.goldstandard, 'sessions_for',
                          side_effect=lambda name: [{'session_id': sid} for sid in by_spelling.get(name, [])]):
            job = self.import_replace()
        self.assertEqual(job['status'], 'completed', job)
        notes = [note for note in job['notes'] if 'gold-standard session(s) from the replaced' in note]
        if expected:
            self.assertEqual(len(notes), 1, job)
            self.assertTrue(notes[0].startswith(f'{expected} gold-standard session(s)'), notes)
        else:
            self.assertEqual(notes, [], job)

    def test_import_replace_note_counts_alias_only_sessions(self):
        self.assert_replace_note_count({'corpus': ['gs_18000003']}, 1)

    def test_import_replace_note_counts_a_session_under_both_spellings_once(self):
        self.assert_replace_note_count({'Corpus': ['gs_18000004'], 'corpus': ['gs_18000004']}, 1)

    def test_import_replace_without_retained_sessions_adds_no_note(self):
        self.assert_replace_note_count({}, 0)

    def test_replace_final_create_and_batch_failure_survive_restart(self):
        for fault in ('create', 'reject', 'partial', 'properties', 'vector'):
            with self.subTest(fault=fault):
                self.cols.create_failure = fault == 'create'
                self.cols.final_fault = None if fault == 'create' else fault
                self.cols.items['Corpus'] = Collection('Corpus')
                job = self.import_replace()
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(job['chunks_written'], 0)
                record = self.recovery_record()
                self.assertEqual(job['error_detail']['recovered_as'], record['staging'])
                self.assertEqual(self.cols.get(record['staging']).rows, {r['id']: r for r in self.original})
                self.assert_retained(record)
                recovery.discard(record, self.client)

    def test_failed_replace_recovery_excludes_unindexed_future_blob(self):
        from services import sources
        pkg, manifest = self.package()
        future = b'future legitimate document'
        digest = hashlib.sha256(future).hexdigest()
        (pkg / 'sources' / digest).write_bytes(b'planted wrong bytes')
        (pkg / 'sources' / 'extra-folder').mkdir()
        self.cols.create_failure = True
        with patch.object(self, 'package', return_value=(pkg, manifest)):
            job = self.import_replace()
        self.assertEqual(job['status'], 'failed')
        recovered = job['error_detail']['recovered_as']
        directory = sources.collection_dir(recovered)
        self.assertEqual({p.name for p in directory.iterdir()},
                         set(sources.load_index(recovered)['documents']) | {'index.json'})
        self.assertFalse((directory / digest).exists())
        sources.store(recovered, 'future.txt', future)
        self.assertEqual((directory / digest).read_bytes(), future)

    def test_replace_restart_after_target_delete_preserves_staging(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.import_replace()
        self.assert_retained(self.recovery_record())

    def test_first_staging_batch_failure_preserves_original_for_import_and_tune(self):
        for operation in ('import', 'tune'):
            with self.subTest(operation=operation):
                self.cols.staging_fault = 'reject'
                before = copy.deepcopy(self.cols.get('Corpus').rows)
                if operation == 'import':
                    job = self.import_replace()
                    self.assertEqual(job['status'], 'failed')
                    self.assertNotIn('recovered_as', job.get('error_detail') or {})
                else:
                    with self.assertRaises(RuntimeError):
                        self.rebuild()
                self.assertEqual(self.cols.get('Corpus').rows, before)
                self.assertFalse(list(recovery._root().glob('*.json')))

    def test_chunks_only_recovery_remains_verified_without_sources(self):
        import shutil
        shutil.rmtree(Path(settings.sources_dir) / 'Corpus')
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        before = copy.deepcopy(self.cols.get(record['staging']).rows)
        recovery.sweep(self.client)
        self.assertEqual(self.cols.get(record['staging']).rows, before)
        self.assertEqual(len(before), len(self.original))

    def test_missing_recovery_backend_does_not_discard_retained_sidecars(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        self.cols.items.pop(record['staging'])
        self.assertEqual(recovery.sweep(self.client), [])
        self.assertTrue((recovery._root() / record['operation_id']).is_dir())
        self.assertTrue((recovery._root() / (record['operation_id'] + '.json')).is_file())

    def test_marker_is_small_and_uses_compact_bounded_expectations(self):
        def stream():
            for i in range(2000):
                yield dict(id=str(uuid.uuid5(uuid.NAMESPACE_OID, str(i))),
                           properties={'content': 'x' * 4096}, vector=[0.25] * 64)
        importer._mark_started('Corpus', 2000, 'job', stream)
        marker = importer._marker_path('Corpus')
        data, snapshot = importer._read_marker(marker)
        self.assertLess(marker.stat().st_size, 1024)
        self.assertNotIn('records', data)
        self.assertLess(snapshot.stat().st_size, 2000 * 512)
        importer._mark_finished('Corpus')
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def import_new_target(self, pkg, manifest):
        # These cases exercise chunk preflight, not evaluation sidecar identity.
        for sidecar in (pkg / 'goldstandard').glob('*.json'):
            sidecar.unlink()
        importer._jobs['preflight'] = {'chunks_written': 0}
        with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                patch.object(importer.packager, 'verify_digests'), \
                patch.object(importer, '_check_embedding'), \
                patch.object(importer, '_ensure_models', return_value=[]), \
                patch.object(importer, '_create_from_package') as create:
            importer._run('preflight', 'fixture.tar.gz', 'abort')
        create.assert_not_called()
        return importer._jobs['preflight']

    def test_new_target_invalid_package_preflight_reports_package_corrupt(self):
        for fault in ('duplicate', 'count', 'timestamp'):
            with self.subTest(fault=fault):
                pkg, manifest = self.package()
                manifest['collection']['name'] = 'NewCorpus'
                rows = json.loads(json.dumps(self.original))
                if fault == 'duplicate':
                    rows[1]['id'] = rows[0]['id']
                elif fault == 'count':
                    manifest['collection']['chunk_count'] = 3
                else:
                    rows[0]['properties']['created_at'] = 'not-a-date'
                (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
                job = self.import_new_target(pkg, manifest)
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(job['error_code'], 'PACKAGE_CORRUPT')
                self.assertEqual(job['error_detail'], {'file': 'chunks.jsonl'})
                self.assertEqual(job['chunks_written'], 0)
                self.assertEqual(list(importer._markers_dir().iterdir()), [])
                self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})

    def test_new_target_snapshot_io_error_remains_import_failed(self):
        pkg, manifest = self.package()
        manifest['collection']['name'] = 'NewCorpus'
        with patch.object(batch_write.ExpectedRecords, 'snapshot', side_effect=OSError('disk unavailable')):
            job = self.import_new_target(pkg, manifest)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'IMPORT_FAILED')
        self.assertIn('disk unavailable', job['error'])
        self.assertEqual(list(importer._markers_dir().iterdir()), [])

    def test_failed_new_target_cleanup_keeps_marker_for_startup_retry(self):
        pkg, manifest = self.package()
        importer._jobs['job'] = {'chunks_written': 0}
        actual_delete = self.cols.delete
        def create_rejected(name, *args, **kwargs):
            self.cols.create(name, description=kwargs.get('description'))
            self.cols.get(name).fault = 'reject'
        def fail_partial_delete(name):
            if '_imported_' in name:
                raise OSError('partial target cleanup unavailable')
            return actual_delete(name)
        with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                patch.object(importer.packager, 'verify_digests'), \
                patch.object(importer, '_check_embedding'), \
                patch.object(importer, '_ensure_models', return_value=[]), \
                patch.object(wc, '_create_collection_sync', side_effect=create_rejected), \
                patch.object(self.cols, 'delete', side_effect=fail_partial_delete):
            importer._run('job', 'fixture.tar.gz', 'rename')
        job = importer._jobs['job']
        self.assertEqual(job['status'], 'failed')
        self.assertTrue(job['error_detail']['cleanup_pending'])
        target = job['error_detail']['collection']
        self.assertTrue(importer._marker_path(target).exists())
        self.assertTrue(self.cols.exists(target))
        self.assertEqual(len(importer.sweep_interrupted_imports()), 1)
        self.assertFalse(self.cols.exists(target))
        self.assertFalse(importer._marker_path(target).exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_oversized_legacy_marker_is_preserved_without_decoding(self):
        marker = importer._marker_path('Corpus')
        marker.write_bytes(b'{' + b'x' * 5000)
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not decode large metadata')):
            self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_corrupt_expected_snapshot_preserves_backend_and_ownership(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        data, snapshot = importer._read_marker(marker)
        snapshot.write_bytes(b'corrupt snapshot')
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_recovery_metadata_cleanup_failure_is_resumed_at_startup(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        with patch.object(recovery.shutil, 'rmtree', side_effect=OSError('metadata cleanup failed')), \
                self.assertRaises(OSError):
            recovery.discard(record, self.client)
        path = recovery._root() / (record['operation_id'] + '.json')
        self.assertEqual(json.loads(path.read_text())['state'], 'cleanup')
        self.assertFalse(self.cols.exists(record['staging']))
        self.assertEqual(recovery.sweep(self.client), [record['staging']])
        self.assertFalse(path.exists())
        self.assertFalse((recovery._root() / record['operation_id']).exists())

    def test_cleanup_intent_is_persisted_before_backend_deletion(self):
        record = recovery.begin('Corpus', 'tune', self.client)
        self.cols.create(record['staging'])
        with patch.object(recovery, '_write', side_effect=OSError('cannot persist cleanup')), \
                self.assertRaises(OSError):
            recovery.discard(record, self.client)
        self.assertTrue(self.cols.exists(record['staging']))
        self.assertEqual(record['state'], 'scratch')

    def test_marker_cleanup_failure_never_deletes_verified_target_on_restart(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        actual_unlink = Path.unlink
        def fail_unlink(path, *args, **kwargs):
            if path == marker:
                raise OSError('marker cleanup failed')
            return actual_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=fail_unlink), self.assertRaises(OSError):
            importer._mark_finished('Corpus')
        self.assertEqual(json.loads(marker.read_text())['state'], 'cleanup')
        self.cols.get('Corpus').rows.clear()  # later legitimate changes must not be swept as partial
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertFalse(marker.exists())

    def created_by_import(self, instance):
        self.cols.get('Corpus').description = importer._instance_description(instance)

    def test_interrupted_import_compares_records_not_only_count(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').rows[self.original[0]['id']]['properties']['content'] = 'mismatch'
        self.assertEqual(len(importer.sweep_interrupted_imports()), 1)
        self.assertFalse(self.cols.exists('Corpus'))

    def test_interrupted_import_read_failure_and_legacy_marker_preserve_data(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').fault = 'read'
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        marker = importer._marker_path('Corpus')
        marker.write_text(json.dumps({'collection': 'Corpus', 'expected_chunks': 1}))
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_new_collection_under_unresolved_marker_name_is_kept(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        _, snapshot = importer._read_marker(marker)
        # A collection the user made after the marker could not be resolved.
        self.cols.get('Corpus').rows[self.original[0]['id']]['properties']['content'] = 'user data'
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def test_unreadable_collection_identity_preserves_collection_and_marker(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').rows.clear()
        def unavailable():
            raise OSError('schema unavailable')
        self.cols.get('Corpus').config = SimpleNamespace(get=unavailable)
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(importer._marker_path('Corpus').exists())

    def legacy_marker(self, state):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        marker = importer._marker_path('Corpus')
        legacy = json.loads(marker.read_text())
        legacy['version'] = 3
        legacy['state'] = state
        legacy.pop('instance')
        marker.write_text(json.dumps(legacy))
        return marker, importer._markers_dir() / legacy['expected_snapshot']['file']

    def test_marker_without_instance_identity_never_deletes(self):
        marker, snapshot = self.legacy_marker('building')
        self.cols.get('Corpus').rows.clear()
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(marker.exists())
        self.assertTrue(snapshot.exists())
        logged = [str(call) for call in importer._log.warning.call_args_list]
        self.assertTrue(any('no instance identity' in line and str(marker) in line for line in logged), logged)
        importer._log.exception.assert_not_called()

    def test_legacy_marker_in_cleanup_is_finished_without_deleting(self):
        marker, snapshot = self.legacy_marker('cleanup')
        self.cols.get('Corpus').rows.clear()
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertNotIn('Corpus', self.cols.deleted)
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def test_import_creates_its_target_with_the_marker_identity(self):
        pkg, manifest = self.package()
        created = {}
        with patch.object(wc, '_create_collection_sync',
                          side_effect=lambda name, *args, **kwargs: created.update({name: kwargs})):
            importer._create_from_package('NewCorpus', pkg, 'a' * 32)
            importer._create_from_package('Staging', pkg)
        self.assertEqual(created['NewCorpus']['description'], importer._instance_description('a' * 32))
        self.assertIsNone(created['Staging']['description'])

    def test_orphaned_expectation_snapshots_are_swept_at_startup(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').fault = 'read'   # keeps the marker and its snapshot
        _, kept = importer._read_marker(importer._marker_path('Corpus'))
        orphan = importer._markers_dir() / (uuid.uuid4().hex + '.sqlite3')
        orphan.write_bytes(b'orphaned by a kill before its marker was written')
        unrelated = importer._markers_dir() / 'notes.sqlite3'
        unrelated.write_bytes(b'not an expectation snapshot name')
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertFalse(orphan.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(unrelated.exists())

    def test_unreadable_marker_blocks_the_orphan_snapshot_sweep(self):
        orphan = importer._markers_dir() / (uuid.uuid4().hex + '.sqlite3')
        orphan.write_bytes(b'may belong to the unreadable marker')
        (importer._markers_dir() / 'Broken.json').write_text('{')
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(orphan.exists())

    def test_job_errors_name_sidecar_snapshots_relative_to_upload_dir(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        record = self.recovery_record()
        self.assertEqual(error.exception.detail['sidecar_snapshots'],
                         'collection_operations/' + record['operation_id'])
        self.cols.create_failure = False
        recovery.discard(record, self.client)
        self.cols.final_fault = 'reject'
        self.cols.items['Corpus'] = Collection('Corpus')
        job = self.import_replace()
        record = self.recovery_record()
        self.assertEqual(job['error_detail']['sidecar_snapshots'],
                         'collection_operations/' + record['operation_id'])
        self.assertTrue((Path(settings.upload_dir) / job['error_detail']['sidecar_snapshots']).is_dir())

    def test_absolute_sidecar_path_goes_to_the_server_log_only(self):
        # Covers tuning and both import failure branches: a general exception
        # (rejected batch) and a PackageError from the final insert.
        actual_insert = importer._insert_chunks
        def package_error(name, *args, **kwargs):
            if name == 'Corpus':
                raise importer.PackageError('IMPORT_FAILED', 'injected final insert failure')
            return actual_insert(name, *args, **kwargs)
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        details = [error.exception.detail]
        records = [self.recovery_record()]
        self.cols.create_failure = False
        recovery.discard(records[-1], self.client)
        for fault in ('reject', 'package_error'):
            with self.subTest(fault=fault):
                self.cols.final_fault = 'reject' if fault == 'reject' else None
                self.cols.items['Corpus'] = Collection('Corpus')
                if fault == 'package_error':
                    with patch.object(importer, '_insert_chunks', side_effect=package_error):
                        job = self.import_replace()
                    self.assertEqual(job['error_code'], 'IMPORT_FAILED')
                    self.assertIn('injected final insert failure', job['error'])
                else:
                    job = self.import_replace()
                details.append(job['error_detail'])
                records.append(self.recovery_record())
                recovery.discard(records[-1], self.client)
        logged = ' '.join(str(call) for call in recovery.log.warning.call_args_list)
        for detail, record in zip(details, records):
            absolute = str(Path(settings.upload_dir) / 'collection_operations' / record['operation_id'])
            self.assertEqual(detail['sidecar_snapshots'], 'collection_operations/' + record['operation_id'])
            self.assertNotIn(settings.upload_dir, json.dumps(detail))
            self.assertIn(absolute, logged)

    def test_sidecar_reference_does_not_depend_on_where_the_root_resolves(self):
        # The reindex verifier keeps recovery records outside UPLOAD_DIR. The
        # reference must still be the documented form, not a failed relative_to.
        with tempfile.TemporaryDirectory() as outside, \
                patch.object(recovery, '_root', return_value=Path(outside) / 'owned'):
            self.assertEqual(recovery.sidecar_reference({'operation_id': 'op1', 'staging': 'S'}),
                             'collection_operations/op1')

    def test_stale_marking_discard_failure_is_logged_and_resumed_at_startup(self):
        def fail_marking():
            raise RuntimeError('session store unavailable')
        with patch.object(recovery.shutil, 'rmtree', side_effect=OSError('metadata cleanup failed')), \
                patch.object(tuning, '_log') as log, self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None,
                            before_replace=fail_marking)
        self.assertIn('original collection is unchanged', error.exception.message)
        log.exception.assert_called_once()
        paths = list(recovery._root().glob('*.json'))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual(record['state'], 'cleanup')
        self.assertEqual(recovery.sweep(self.client), [record['staging']])
        self.assertEqual(list(recovery._root().iterdir()), [])
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertNotIn('Corpus', self.cols.deleted)

    def test_stale_marking_failure_before_cutover_discards_copy_and_keeps_original(self):
        def fail_marking():
            raise RuntimeError('session store unavailable')
        before = copy.deepcopy(self.cols.get('Corpus').rows)
        with self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None,
                            before_replace=fail_marking)
        self.assertEqual(error.exception.code, 'TUNE_FAILED')
        self.assertIn('could not be marked stale', error.exception.message)
        self.assertIn('original collection is unchanged', error.exception.message)
        self.assertNotIn('recovered_as', error.exception.detail or {})
        self.assertEqual(self.cols.get('Corpus').rows, before)
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertEqual(list(recovery._root().iterdir()), [])

    # ── reviewer-added (#126 item 1, 7) ──

    def test_every_import_mode_binds_its_target_to_the_marker_instance(self):
        seen = {}
        actual_finish = importer._mark_finished
        def finish(collection):
            data, _ = importer._read_marker(importer._marker_path(collection))
            others = {name: col.description for name, col in self.cols.items.items() if name != collection}
            seen[collection] = (data['version'], data['instance'],
                                self.cols.get(collection).description, others)
            actual_finish(collection)
        for mode in ('abort', 'rename', 'replace'):
            with self.subTest(mode=mode):
                seen.clear()
                self.cols.items.clear()
                if mode != 'abort':
                    self.cols.create('Corpus')
                pkg, manifest = self.package()
                importer._jobs['job'] = dict(chunks_written=0)
                with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                        patch.object(importer.packager, 'verify_digests'), \
                        patch.object(importer, '_check_embedding'), \
                        patch.object(importer, '_ensure_models', return_value=[]), \
                        patch.object(importer, '_mark_finished', side_effect=finish):
                    importer._run('job', 'fixture.tar.gz', mode)
                job = importer._jobs['job']
                self.assertEqual(job['status'], 'completed', job)
                self.assertEqual(list(seen), [job['collection']])
                version, instance, description, others = seen[job['collection']]
                self.assertEqual(version, 4)
                self.assertRegex(instance, r'^[0-9a-f]{32}$')
                self.assertEqual(description, importer._instance_description(instance))
                # Staging copies (replace) and pre-existing collections carry no token.
                self.assertTrue(all(value is None for value in others.values()), others)

    def test_v4_marker_with_invalid_instance_never_deletes(self):
        for bad in ('A' * 32, 'a' * 31, 7, None, '<missing>'):
            with self.subTest(instance=bad):
                self.cols.items['Corpus'] = Collection('Corpus')
                batch_write.insert(self.cols.get('Corpus'), self.original)
                importer._mark_started('Corpus', 2, 'job', self.original)
                marker = importer._marker_path('Corpus')
                data = json.loads(marker.read_text())
                if bad == '<missing>':
                    data.pop('instance')
                else:
                    data['instance'] = bad
                marker.write_text(json.dumps(data))
                if isinstance(bad, str):
                    self.cols.get('Corpus').description = importer._instance_description(bad)
                self.cols.get('Corpus').rows.clear()   # would be deleted if the marker were trusted
                self.assertEqual(importer.sweep_interrupted_imports(), [])
                self.assertTrue(self.cols.exists('Corpus'))
                self.assertNotIn('Corpus', self.cols.deleted)
                self.assertTrue(marker.exists())
                self.assertTrue((importer._markers_dir() / data['expected_snapshot']['file']).exists())
                marker.unlink()

    def test_other_import_token_keeps_collection_and_retires_marker(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        _, snapshot = importer._read_marker(marker)
        self.cols.get('Corpus').description = importer._instance_description(uuid.uuid4().hex)
        self.cols.get('Corpus').rows.clear()
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertNotIn('Corpus', self.cols.deleted)
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def test_orphan_sweep_keeps_snapshots_named_by_invalid_markers_and_symlinks(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        data = json.loads(marker.read_text())
        data['version'] = 2          # parseable, but not valid ownership: preserved by the sweep
        marker.write_text(json.dumps(data))
        named = importer._markers_dir() / data['expected_snapshot']['file']
        target = self.root / 'outside.sqlite3'
        target.write_bytes(b'outside the markers directory')
        link = importer._markers_dir() / (uuid.uuid4().hex + '.sqlite3')
        link.symlink_to(target)
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(named.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())

    def test_import_final_flush_never_reports_attempts_as_written(self):
        pkg = self.root / 'package'
        pkg.mkdir()
        (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.original))
        manifest = {'collection': {'chunk_count': 2}, 'embedding': {'dimensions': 2}}
        self.cols.get('Corpus').rows.clear()
        self.cols.get('Corpus').fault = 'reject'
        progress = []
        with self.assertRaises(RuntimeError):
            importer._insert_chunks('Corpus', pkg, manifest, progress.append)
        self.assertEqual(progress, [])


class SourceCoverageTests(unittest.TestCase):
    def test_rechunk_requires_unambiguous_originals_for_all_stored_files(self):
        from services import sources
        for operation in ("rechunk", "reembed"):
            for missing in ("legacy.txt", None, "retained.txt"):
                with self.subTest(operation=operation, missing=missing), tempfile.TemporaryDirectory() as tmp:
                    cols = Collections()
                    cols.create("Corpus")
                    original = records(2)
                    original[0]["properties"]["source_file"] = missing
                    original[1]["properties"]["source_file"] = "retained.txt"
                    cols.get("Corpus").rows = {r["id"]: copy.deepcopy(r) for r in original}
                    with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)):
                        sources.store("Corpus", "retained.txt", b"retained original")
                        if missing == "retained.txt":
                            sources.store("Corpus", "retained.txt", b"ambiguous second original")
                        job = {}
                        with patch.dict(tuning._jobs, {"coverage": job}), patch.object(tuning, "_parse_file") as parse:
                            tuning._run("coverage", "Corpus", operation, {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                        self.assertEqual(job["status"], "failed")
                        self.assertEqual(job["error_code"], "SOURCES_REQUIRED")
                        parse.assert_not_called()
                        self.assertEqual(cols.get("Corpus").rows, {r["id"]: r for r in original})
                        self.assertEqual(cols.deleted, [])
                        self.assertEqual(set(cols.items), {"Corpus"})
                        self.assertEqual(list(recovery._root().glob("*.json")), [])

    def test_fully_retained_collection_completes_real_cutover(self):
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            cols.create("Corpus")
            original = records(1)[0]
            original["properties"]["source_file"] = "retained.txt"
            cols.get("Corpus").rows[original["id"]] = original
            with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(wc, "_create_collection_sync", side_effect=lambda name, *a, **kw: cols.create(name)), patch.object(wc, "_collection_config_sync", return_value={"index_type": "hnsw", "distance_metric": "cosine", "hnsw_config": {}}), patch.object(tuning, "_parse_file", side_effect=lambda p: (p.read_text(), [])), patch.object(tuning, "do_chunk", side_effect=lambda **kw: [kw["text"]]):
                sources.store("Corpus", "retained.txt", b"retained original")
                job = {}
                with patch.dict(tuning._jobs, {"coverage": job}):
                    tuning._run("coverage", "Corpus", "rechunk", {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual([r["properties"]["content"] for r in cols.get("Corpus").rows.values()], ["retained original"])
                self.assertEqual(set(cols.items), {"Corpus"})
                self.assertEqual(list(recovery._root().glob("*.json")), [])


    # Each case: retained originals as (name, bytes), stored chunks as
    # (source_file, chunk_index) with None for no chunk_index, and the
    # names the coverage rule must report.
    DROP_CASES = {
        # Stored before #22 made re-ingesting replace: one name, two versions.
        "a name with two retained versions": (
            [("a.txt", b"v1"), ("a.txt", b"v2")], [("a.txt", 0), ("a.txt", 1)], ["a.txt"]),
        "re-upload whose retention failed": (
            [("a.txt", b"v1")], [("a.txt", 0), ("a.txt", 1), ("a.txt", 0), ("a.txt", 1)], ["a.txt"]),
        "identical re-upload, retained twice": (
            [("a.txt", b"same"), ("a.txt", b"same")], [("a.txt", 0), ("a.txt", 0)], ["a.txt"]),
        "two chunks with no chunk_index": (
            [("a.txt", b"v1")], [("a.txt", None), ("a.txt", None)], ["a.txt"]),
    }

    @staticmethod
    def corpus(cols, chunks):
        cols.create("Corpus")
        rows = records(len(chunks))
        for row, (name, index) in zip(rows, chunks):
            row["properties"]["source_file"] = name
            if index is not None:
                row["properties"]["chunk_index"] = index
        cols.get("Corpus").rows = {r["id"]: copy.deepcopy(r) for r in rows}
        return rows

    def test_rechunk_refuses_names_it_would_drop(self):
        from services import sources
        chunking = {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}
        for operation in ("rechunk", "reembed"):
            for case, (retained, chunks, expected) in self.DROP_CASES.items():
                with self.subTest(operation=operation, case=case), tempfile.TemporaryDirectory() as tmp:
                    cols = Collections()
                    original = self.corpus(cols, chunks)
                    with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)):
                        for name, data in retained:
                            sources.store("Corpus", name, data)
                        job = {}
                        with patch.dict(tuning._jobs, {"coverage": job}), patch.object(tuning, "_parse_file") as parse:
                            tuning._run("coverage", "Corpus", operation, {"chunking": dict(chunking)})
                        self.assertEqual(job["status"], "failed")
                        self.assertEqual(job["error_code"], "SOURCES_REQUIRED")
                        self.assertEqual(job["error_detail"]["uncovered_source_files"], expected)
                        parse.assert_not_called()
                        self.assertEqual(cols.get("Corpus").rows, {r["id"]: r for r in original})
                        self.assertEqual(cols.deleted, [])
                        self.assertEqual(set(cols.items), {"Corpus"})
                        self.assertEqual(list(recovery._root().glob("*.json")), [])

    def test_single_chunk_set_per_name_completes(self):
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            self.corpus(cols, [("a.txt", 0), ("a.txt", 1), ("a.txt", 2)])
            with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(wc, "_create_collection_sync", side_effect=lambda name, *a, **kw: cols.create(name)), patch.object(wc, "_collection_config_sync", return_value={"index_type": "hnsw", "distance_metric": "cosine", "hnsw_config": {}}), patch.object(tuning, "_parse_file", side_effect=lambda p: (p.read_text(), [])), patch.object(tuning, "do_chunk", side_effect=lambda **kw: [kw["text"]]):
                sources.store("Corpus", "a.txt", b"retained original")
                job = {}
                with patch.dict(tuning._jobs, {"coverage": job}):
                    tuning._run("coverage", "Corpus", "rechunk", {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual([r["properties"]["source_file"] for r in cols.get("Corpus").rows.values()], ["a.txt"])


    def test_second_name_for_the_same_content_is_rebuilt_under_both_names(self):
        # #22 lifted this refusal: one chunk set per (digest, name), as ingested.
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            self.corpus(cols, [("a.txt", 0), ("docs/b.txt", 0)])
            with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(wc, "_create_collection_sync", side_effect=lambda name, *a, **kw: cols.create(name)), patch.object(wc, "_collection_config_sync", return_value={"index_type": "hnsw", "distance_metric": "cosine", "hnsw_config": {}}), patch.object(tuning, "_parse_file", side_effect=lambda p: (p.read_text(), [])), patch.object(tuning, "do_chunk", side_effect=lambda **kw: [kw["text"]]):
                sources.store("Corpus", "a.txt", b"same original")
                sources.store("Corpus", "docs/b.txt", b"same original")
                job = {}
                with patch.dict(tuning._jobs, {"coverage": job}):
                    tuning._run("coverage", "Corpus", "rechunk", {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                self.assertEqual(job["status"], "completed", job)
                rows = cols.get("Corpus").rows.values()
                self.assertEqual(sorted(r["properties"]["source_file"] for r in rows), ["a.txt", "docs/b.txt"])
                self.assertEqual({r["properties"]["content"] for r in rows}, {"same original"})

    def test_tune_options_match_the_refusal(self):
        from routers import tuning as tuning_router
        from services import sources
        chunks_only = ("No original documents were retained, so this collection cannot be "
                       "re-chunked. Re-embedding works from the stored chunk text, which "
                       "leaves chunk boundaries unchanged.")
        refused = ("Some stored source files have no single retained original, or a "
                   "retained original is missing, so this collection cannot be "
                   "re-chunked. Re-embedding works from the stored chunk text, which "
                   "leaves chunk boundaries unchanged, and re-indexing is available.")
        nothing_to_rebuild = ("No retained original file has stored chunks, so this collection "
                              "cannot be re-chunked: there is nothing to rebuild. Re-embedding works "
                              "from the stored chunk text, which leaves chunk boundaries unchanged, "
                              "and re-indexing is available.")
        two_versions = self.DROP_CASES["a name with two retained versions"]
        repeated_set = self.DROP_CASES["re-upload whose retention failed"]
        cases = {
            "nothing retained": ([], [("a.txt", 0)], False, chunks_only, False),
            "fully covered": ([("a.txt", b"v1")], [("a.txt", 0), ("a.txt", 1)], True,
                              "Every tuning operation is available.", False),
            "second name for the same content": ([("a.txt", b"same"), ("b.txt", b"same")],
                                                  [("a.txt", 0), ("b.txt", 0)], True,
                                                  "Every tuning operation is available.", False),
            "a name with two retained versions": (two_versions[0], two_versions[1], False, refused, False),
            "repeated chunk set": (repeated_set[0], repeated_set[1], False, refused, False),
            "retained original missing from disk": ([("a.txt", b"v1")], [("a.txt", 0)], False, refused, True),
            # #328: every retained file produced no chunks; the reason comes
            # first, as the start refuses it before the others.
            "retained originals with no stored chunks": ([("a.txt", b"v1")], [], False,
                                                         nothing_to_rebuild, False),
            "nothing to rebuild and a blob missing": ([("a.txt", b"v1")], [("b.txt", 0)], False,
                                                      nothing_to_rebuild, True),
        }
        for case, (retained, chunks, can_rechunk, note, drop_blob) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                cols = Collections()
                self.corpus(cols, chunks)
                with patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(tuning_router.wc, "collection_exists", new_callable=AsyncMock, return_value=True):
                    for name, data in retained:
                        digest = sources.store("Corpus", name, data)
                    if drop_blob:
                        sources.blob_path("Corpus", digest).unlink()
                    response = asyncio.run(tuning_router.tune_options("Corpus"))
                self.assertEqual(response.can_rechunk, can_rechunk)
                self.assertEqual(response.note, note)
                self.assertEqual(response.fidelity, "with-sources" if retained else "chunks-only")


if __name__ == '__main__':
    unittest.main()
