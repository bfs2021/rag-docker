"""Controlled package preflight and generated-script trust-boundary regressions."""
import ast
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from models.schemas import SaveRetrievalConfigBody
from services import importer, model_bundle, packager, retrieval_config


INVALID = [None, [], 3, 'settings', {'top_k': '5; injected = True'},
           {'retrieval_mode': 'hybrid"; injected = True #'},
           {'response_format': 'engineer"; injected = True #'},
           {'top_k': True}, {'top_k': 0}, {'top_k': 51}, {'top_k': 1.5},
           {'alpha': False}, {'alpha': -0.1}, {'alpha': 1.1},
           {'alpha': float('nan')}, {'alpha': float('inf')},
           {'ef': True}, {'ef': '10000'}, {'ef': 64.5}]
# Integer ef values outside 16-512 that the API stored before PR #108. ef is
# inactive, so export and import clear them to null instead of refusing.
LEGACY_EF = [-1, 0, 15, 513, 10000]
EXPORT_WARNING = ('saved retrieval setting ef={} is outside 16-512 and was exported as null; '
                  "ef is no longer used. Save this collection's settings on the Retrieval "
                  'page to clear it.')
IMPORT_NOTE = ("the package's retrieval setting ef={} is outside 16-512 and was restored "
               'as null; ef is no longer used.')


class RetrievalImportTests(unittest.TestCase):
    def test_export_job_preserves_package_refusal_through_response_schema(self):
        from services import exporter
        from models.schemas import ExportJobStatusResponse
        job = dict(job_id='owned', collection='Owned', status='queued', chunks_written=0,
                   filename=None, size_bytes=None, source_document_count=None,
                   fidelity=None, models_bundled=None, retrieve_script=None,
                   warnings=[], error=None)
        error = packager.PackageError('EMBEDDING_MISMATCH', 'Re-embed before exporting.', {'model': 'other'})
        with patch.object(exporter, '_jobs', {'owned': job}), \
             patch.object(exporter, '_active', {'Owned': 'owned'}), \
             patch.object(packager, 'build', side_effect=error), \
             patch.object(exporter._log, 'exception') as traceback:
            exporter._run('owned', 'Owned', False)
            result = ExportJobStatusResponse(**job).model_dump()
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_code'], 'EMBEDDING_MISMATCH')
            self.assertEqual(result['error_detail'], {'model': 'other'})
            self.assertIn('Re-embed', result['error'])
            traceback.assert_not_called()
            self.assertNotIn('Owned', exporter._active)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / 'package'
        self.pkg.mkdir()
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        self.exports = self.root / 'exports'
        self.exports.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in [('upload_dir', self.uploads), ('exports_dir', self.exports),
                           ('sources_dir', self.root / 'sources')]:
            self.stack.enter_context(patch.object(settings, key, str(value)))
        self.stack.enter_context(patch.object(retrieval_config, '_DIR', None))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.object(importer, '_log'))

    def archive(self, data):
        (self.pkg / 'retrieval_config.json').write_text(data)
        (self.pkg / 'chunks.jsonl').write_text('')
        manifest = {'package_format': 1, 'collection': {'name': 'Corpus', 'chunk_count': 0},
                    'embedding': {'model': settings.embed_model},
                    'files': {p.name: 'sha256:' + packager.sha256_file(p)
                              for p in self.pkg.iterdir() if p.name != 'manifest.json'}}
        (self.pkg / 'manifest.json').write_text(json.dumps(manifest))
        with tarfile.open(self.exports / 'fixture.tar.gz', 'w:gz') as tar:
            tar.add(self.pkg, arcname='package')

    def test_digest_valid_bad_settings_refused_before_any_live_mutation(self):
        original = retrieval_config.validate({'top_k': 9}, 'Corpus')
        retrieval_config.save(original)
        saved = retrieval_config._path('Corpus')
        before = saved.read_bytes()
        for data in [json.dumps(value) for value in INVALID] + ['{']:
            self.archive(data)
            for conflict in ('abort', 'rename', 'replace'):
                with self.subTest(data=data, conflict=conflict), ExitStack() as mocks:
                    operations = [mocks.enter_context(patch.object(module, name)) for module, name in [
                        (importer, '_ensure_models'), (importer, '_build'),
                        (importer.wc, '_collection_exists_sync'), (importer.wc, '_delete_collection_sync'),
                        (importer.wc, 'get_client'), (importer, '_restore_sidecars'),
                        (importer, '_mark_started'), (importer.collection_recovery, 'begin')]]
                    importer._jobs['test'] = {'status': 'queued'}
                    importer._run('test', 'fixture.tar.gz', conflict)
                    result = importer._jobs['test']
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['error_code'], 'PACKAGE_CORRUPT')
                    self.assertEqual(result['error_detail'], {'file': 'retrieval_config.json'})
                    for operation in operations:
                        operation.assert_not_called()
                    self.assertEqual(saved.read_bytes(), before)
                    self.assertFalse((self.root / 'sources').exists())
                    self.assertFalse(list(self.uploads.glob('import-*')))

    def test_valid_historical_defaults_coercion_and_ef_match_api_and_roundtrip(self):
        for data in [{}, {'collection': 'OldName', 'top_k': '7', 'alpha': '0.5', 'ef': '64'},
                     {'retrieval_mode': 'flat', 'top_k': 50, 'alpha': 0, 'ef': 512,
                      'response_format': 'engineer', 'legacy_field': 'ignored'}]:
            with self.subTest(data=data):
                self.archive(json.dumps(data))
                expected = SaveRetrievalConfigBody.model_validate({**data, 'collection': 'Corpus'}).model_dump()
                with patch.object(importer, '_ensure_models', return_value=[]), \
                     patch.object(importer.wc, '_collection_exists_sync', return_value=False), \
                     patch.object(importer, '_build', return_value=0):
                    importer._jobs['test'] = {'status': 'queued'}
                    importer._run('test', 'fixture.tar.gz', 'abort')
                self.assertEqual(importer._jobs['test']['status'], 'completed')
                self.assertEqual(retrieval_config.load('Corpus'), expected)

    def test_legacy_ef_is_normalised_and_reported(self):
        for value in LEGACY_EF:
            with self.subTest(ef=value):
                cfg, cleared = retrieval_config.normalize({'ef': value, 'top_k': 7}, 'Corpus')
                self.assertIsNone(cfg['ef'])
                self.assertEqual(cfg['top_k'], 7)
                self.assertEqual(cleared, value)
        for value, kept in [(16, 16), (512, 512), (64, 64), (None, None), ('64', 64)]:
            with self.subTest(ef=value):
                cfg, cleared = retrieval_config.normalize({'ef': value}, 'Corpus')
                self.assertEqual(cfg['ef'], kept)
                self.assertIsNone(cleared)
                self.assertEqual(retrieval_config.validate({'ef': value}, 'Corpus'), cfg)

    def test_legacy_ef_import_completes_with_note(self):
        # Every conflict mode (#184): abort into a new collection, rename beside
        # an existing one, and replace an existing one through staging.
        for conflict in ('abort', 'rename', 'replace'):
            for value in LEGACY_EF:
                with self.subTest(conflict=conflict, ef=value), ExitStack() as mocks:
                    self.archive(json.dumps({'ef': value, 'top_k': 7}))
                    mocks.enter_context(patch.object(importer, '_ensure_models', return_value=[]))
                    mocks.enter_context(patch.object(importer, '_build', return_value=0))
                    mocks.enter_context(patch.object(
                        importer.wc, '_collection_exists_sync',
                        side_effect=lambda name: conflict != 'abort' and name == 'Corpus'))
                    if conflict == 'replace':
                        mocks.enter_context(patch.object(importer.collection_recovery, 'begin',
                                                         return_value={'staging': 'Corpus__importing_test',
                                                                       'state': 'scratch'}))
                        for name in ('retain', 'discard'):
                            mocks.enter_context(patch.object(importer.collection_recovery, name))
                        mocks.enter_context(patch.object(importer.wc, 'get_client'))
                        mocks.enter_context(patch.object(importer.wc, '_delete_collection_sync'))
                        mocks.enter_context(patch.object(importer.goldstandard, 'sessions_for', return_value=[]))
                    importer._jobs['test'] = {'status': 'queued'}
                    importer._run('test', 'fixture.tar.gz', conflict)
                    job = importer._jobs['test']
                    self.assertEqual(job['status'], 'completed', job)
                    target = job['collection']
                    if conflict == 'rename':
                        self.assertTrue(target.startswith('Corpus_imported_'), target)
                    else:
                        self.assertEqual(target, 'Corpus')
                    restored = retrieval_config.load(target)
                    self.assertIsNone(restored['ef'])
                    self.assertEqual(restored['top_k'], 7)
                    self.assertEqual(job['notes'].count(IMPORT_NOTE.format(value)), 1)
                    retrieval_config._path(target).unlink()

    def test_restore_uses_preflight_snapshot_and_rebinds_renamed_collection(self):
        self.archive(json.dumps({'top_k': '8', 'ef': 64}))
        validated = importer._read_retrieval_config(self.pkg, 'Corpus')
        (self.pkg / 'retrieval_config.json').write_text('{')
        importer._restore_sidecars('Renamed', self.pkg, 'Corpus', [], validated_retrieval=validated)
        self.assertEqual(retrieval_config.load('Renamed'), {**validated, 'collection': 'Renamed'})
        self.assertEqual(validated['collection'], 'Corpus')

    def test_absent_settings_remain_optional_and_nonregular_settings_fail(self):
        self.assertIsNone(importer._read_retrieval_config(self.pkg, 'Corpus'))
        (self.pkg / 'retrieval_config.json').mkdir()
        with self.assertRaises(packager.PackageError):
            importer._read_retrieval_config(self.pkg, 'Corpus')

    def test_saved_invalid_settings_cannot_generate_script(self):
        for data in INVALID:
            with self.subTest(data=data), self.assertRaises(ValueError):
                packager._render_retrieve('Corpus', data, {})

    def test_generated_script_defaults_are_literals_and_metadata_stays_data(self):
        malicious = '\"\"\"\nraise RuntimeError("injected")\n#\\\n@@TOP_K@@'
        cfg = {'retrieval_mode': 'hybrid', 'top_k': '8', 'alpha': '0.25',
               'response_format': 'engineer', 'ef': 64, 'unknown': malicious}
        source = packager._render_retrieve(malicious, cfg, {'embed_model': malicious})
        tree = ast.parse(source)
        literals = {node.targets[0].id: ast.literal_eval(node.value)
                    for node in tree.body if isinstance(node, ast.Assign)}
        self.assertEqual(literals['COLLECTION'], malicious)
        self.assertEqual(literals['PACKAGE_METADATA'], {'embed_model': malicious})
        self.assertEqual(literals['DEFAULT_TOP_K'], 8)
        self.assertIs(type(literals['DEFAULT_TOP_K']), int)
        self.assertEqual(literals['DEFAULT_ALPHA'], 0.25)
        namespace = {'__name__': 'retrieval_import_test'}
        exec(compile(tree, 'retrieve.py', 'exec'), namespace)
        args = namespace['build_parser']().parse_args(['a question'])
        self.assertEqual((args.mode, args.top_k, args.alpha, args.response_format),
                         ('hybrid', 8, 0.25, 'engineer'))

    def test_export_checks_stored_model_before_streaming_or_bundling(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        for model, kind, named in (('old-model', 'text2vec-ollama', None), (None, 'none', None), ('new-model', 'text2vec-ollama', {'named': {}})):
            with self.subTest(model=model, kind=kind, named=named):
                cfg = SimpleNamespace(vector_index_config=SimpleNamespace(), properties=[], vectorizer_config=SimpleNamespace(vectorizer=kind, model={'model': model}), vector_config=named)
                backend = MagicMock()
                backend.collections.get.return_value.config.get.return_value = cfg
                with patch.object(settings, 'embed_model', 'new-model'), patch.object(packager.wc, 'get_client', return_value=backend), patch.object(packager, 'read_chunks') as chunks, patch.object(packager.model_bundle, 'export_model') as bundle:
                    before = sorted(self.exports.iterdir())
                    with self.assertRaises(packager.PackageError) as caught:
                        packager.build('Corpus', include_models=True)
                    self.assertEqual(caught.exception.code, 'EMBEDDING_MISMATCH')
                    self.assertIn('Re-embed', caught.exception.message)
                    chunks.assert_not_called()
                    bundle.assert_not_called()
                    self.assertEqual(sorted(self.exports.iterdir()), before)

    def test_collection_schema_records_actual_model_not_process_default(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        backend = MagicMock()
        backend.collections.get.return_value.config.get.return_value = SimpleNamespace(vector_index_config=SimpleNamespace(), properties=[], vectorizer_config=SimpleNamespace(vectorizer='text2vec-ollama', model={'model': 'stored-model'}), vector_config=None)
        with patch.object(settings, 'embed_model', 'different-model'), patch.object(packager.wc, 'get_client', return_value=backend):
            self.assertEqual(packager.wc._collection_config_sync('Corpus')['embedding_model'], 'stored-model')

    def test_export_emits_normalized_settings_and_executable_defaults(self):
        saved = {'collection': 'Corpus', 'top_k': '7', 'alpha': '0.5', 'ef': 64}
        retrieval_config.save(saved)
        with patch.object(packager, 'read_chunks', return_value=[]), \
             patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': settings.embed_model}), \
             patch.object(packager, '_ingest_config', return_value=None), \
             patch.object(packager, '_goldstandard_sessions', return_value=[]), \
             patch.object(packager.sources, 'load_index', return_value={'documents': {}}), \
             patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}):
            result = packager.build('Corpus')
        with tarfile.open(self.exports / result['filename']) as archive:
            members = {Path(m.name).name: m for m in archive.getmembers() if m.isfile()}
            cfg = json.load(archive.extractfile(members['retrieval_config.json']))
            source = archive.extractfile(members['retrieve.py']).read().decode()
        self.assertEqual(cfg, SaveRetrievalConfigBody.model_validate(saved).model_dump())
        namespace = {'__name__': 'retrieval_import_test'}
        exec(compile(source, 'retrieve.py', 'exec'), namespace)
        self.assertEqual(namespace['DEFAULT_TOP_K'], 7)
        self.assertEqual(namespace['DEFAULT_ALPHA'], 0.5)
        self.assertEqual(namespace['PACKAGE_METADATA']['package_filename'], result['filename'])

    def test_export_clears_legacy_ef_with_warning(self):
        retrieval_config.save({'collection': 'Corpus', 'top_k': 7, 'ef': 10000})
        saved = retrieval_config._path('Corpus')
        before = saved.read_bytes()
        with patch.object(packager, 'read_chunks', return_value=[]), \
             patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': settings.embed_model}), \
             patch.object(packager, '_ingest_config', return_value=None), \
             patch.object(packager, '_goldstandard_sessions', return_value=[]), \
             patch.object(packager.sources, 'load_index', return_value={'documents': {}}), \
             patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}):
            result = packager.build('Corpus')
        with tarfile.open(self.exports / result['filename']) as archive:
            members = {Path(m.name).name: m for m in archive.getmembers() if m.isfile()}
            cfg = json.load(archive.extractfile(members['retrieval_config.json']))
            manifest = json.load(archive.extractfile(members['manifest.json']))
        self.assertIsNone(cfg['ef'])
        self.assertEqual(cfg['top_k'], 7)
        self.assertIn('retrieve.py', members)
        warning = EXPORT_WARNING.format(10000)
        self.assertEqual(result['warnings'].count(warning), 1)
        self.assertEqual(manifest['warnings'].count(warning), 1)
        self.assertEqual(saved.read_bytes(), before)

    def build_with_models(self, failing, llm_model='chat:v2', include_models=True,
                          store_available=True, broken_copy=(), fail_mid_write=(),
                          embed_model='embed:v1'):
        """Export with both models requested; models named in `failing` cannot bundle.

        Models named in `broken_copy` resolve, but their second file's copy fails.
        Models named in `fail_mid_write` resolve, but their second file's copy
        fails after writing part of it. `self.exported` records each resolution."""
        blob = self.root / 'model-blob'
        blob.write_text('inert model bytes')
        torn = self.root / 'torn-blob'
        torn.write_text('bytes that never finish copying')
        self.exported = []
        copyfile = packager.shutil.copyfile

        def export_model(model, stage):
            self.exported.append(model)
            if model in failing:
                raise FileNotFoundError(f"Ollama has no manifest for '{model}'")
            folder = 'models/' + '/'.join(model_bundle.split_ref(model))
            files = [(f"{folder}/manifest.json", blob)]
            if model in broken_copy:
                files.append((f"{folder}/blobs/sha256-gone",
                              self.root / 'missing-blob'))
            if model in fail_mid_write:
                files.append((f"{folder}/blobs/sha256-torn", torn))
            return files

        def copy_or_tear(source, target):
            if Path(source) == torn:
                Path(target).write_text('bytes that')
                raise OSError(28, 'No space left on device')
            return copyfile(source, target)

        with ExitStack() as mocks:
            mocks.enter_context(patch.object(settings, 'embed_model', embed_model))
            mocks.enter_context(patch.object(settings, 'llm_model', llm_model))
            mocks.enter_context(patch.object(packager, 'read_chunks', return_value=[]))
            mocks.enter_context(patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': embed_model}))
            mocks.enter_context(patch.object(packager, '_ingest_config', return_value=None))
            mocks.enter_context(patch.object(packager, '_goldstandard_sessions', return_value=[]))
            mocks.enter_context(patch.object(packager.sources, 'load_index', return_value={'documents': {}}))
            mocks.enter_context(patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}))
            mocks.enter_context(patch.object(packager.model_bundle, 'store_available', return_value=store_available))
            mocks.enter_context(patch.object(packager.model_bundle, 'export_model', side_effect=export_model))
            mocks.enter_context(patch.object(packager.shutil, 'copyfile', side_effect=copy_or_tear))
            result = packager.build('Corpus', include_models=include_models)
        with tarfile.open(self.exports / result['filename']) as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith('/manifest.json')
                          and m.name.count('/') == 1)
            manifest = json.load(archive.extractfile(member))
        return result, manifest

    def extracted(self, result):
        """The package as import reads it: `open_package` into a fresh directory."""
        dest = Path(tempfile.mkdtemp(dir=self.root))
        return packager.open_package(self.exports / result['filename'], dest)

    def test_a_failed_model_copy_leaves_no_part_of_that_model(self):
        for broken in ({'broken_copy': {'chat:v2'}}, {'fail_mid_write': {'chat:v2'}}):
            with self.subTest(**broken):
                for leftover in self.exports.iterdir():
                    leftover.unlink()
                result, manifest = self.build_with_models(set(), **broken)
                pkg, _ = self.extracted(result)
                self.assertFalse((pkg / 'models' / 'chat').exists())
                self.assertFalse(any(k.startswith('models/chat/') for k in manifest['files']))
                self.assertIn('models/embed/v1/manifest.json', manifest['files'])
                self.assertTrue((pkg / 'models' / 'embed' / 'v1' / 'manifest.json').is_file())
                self.assertEqual(model_bundle.bundled_models(pkg), ['embed:v1'])
                self.assertTrue(any(w.startswith("model 'chat:v2' could not be bundled")
                                    for w in manifest['warnings']))

    def test_a_failed_first_model_leaves_the_next_one_whole(self):
        result, manifest = self.build_with_models(set(), fail_mid_write={'embed:v1'})
        pkg, _ = self.extracted(result)
        self.assertFalse((pkg / 'models' / 'embed').exists())
        self.assertFalse(any(k.startswith('models/embed/') for k in manifest['files']))
        self.assertEqual(model_bundle.bundled_models(pkg), ['chat:v2'])
        self.assertEqual(manifest['bundled_models'], ['chat:v2'])

    def test_every_model_failing_leaves_no_models_directory(self):
        result, manifest = self.build_with_models(
            set(), broken_copy={'embed:v1'}, fail_mid_write={'chat:v2'})
        pkg, _ = self.extracted(result)
        self.assertFalse((pkg / 'models').exists())
        self.assertFalse(any(k.startswith('models/') for k in manifest['files']))
        self.assertEqual(manifest['bundled_models'], [])
        self.assertIs(manifest['models_bundled'], False)
        self.assertEqual(sum(w.startswith('model ') and 'could not be bundled' in w
                             for w in manifest['warnings']), 2)

    def test_a_model_shared_by_embedding_and_llm_is_copied_once(self):
        result, manifest = self.build_with_models(set(), llm_model='embed:v1')
        self.assertEqual(self.exported, ['embed:v1'])
        self.assertEqual(manifest['bundled_models'], ['embed:v1'])

    def test_a_model_named_with_and_without_its_tag_is_one_model(self):
        # `embed` and `embed:latest` share models/embed/latest/; a second copy
        # failing used to delete the first's files while the manifest kept
        # listing the model (#494).
        # The second spelling failing is the case that lost the first's files.
        for failing in (set(), {'embed:latest'}):
            with self.subTest(fail_mid_write=sorted(failing)):
                for leftover in self.exports.iterdir():
                    leftover.unlink()
                result, manifest = self.build_with_models(set(), embed_model='embed',
                                                          llm_model='embed:latest',
                                                          fail_mid_write=failing)
                pkg, _ = self.extracted(result)
                self.assertEqual(self.exported, ['embed'])
                self.assertEqual(manifest['bundled_models'], ['embed:latest'])
                self.assertIn('models/embed/latest/manifest.json', manifest['files'])
                self.assertEqual(model_bundle.bundled_models(pkg), ['embed:latest'])
                self.assertFalse(any('could not be bundled' in w for w in manifest['warnings']))
        for leftover in self.exports.iterdir():
            leftover.unlink()
        with self.subTest(fail_mid_write=['embed']):
            result, manifest = self.build_with_models(set(), embed_model='embed',
                                                      llm_model='embed:latest',
                                                      fail_mid_write={'embed'})
            pkg, _ = self.extracted(result)
            self.assertEqual(self.exported, ['embed'])
            self.assertEqual(manifest['bundled_models'], [])
            self.assertIs(manifest['models_bundled'], False)
            self.assertFalse(any(k.startswith('models/') for k in manifest['files']))
            self.assertFalse((pkg / 'models').exists())
            failed = [w for w in manifest['warnings'] if 'could not be bundled' in w]
            self.assertEqual(len(failed), 1, failed)
            self.assertTrue(failed[0].startswith("model 'embed' could not be bundled"))

    def test_manifest_lists_no_models_when_none_were_requested_or_the_store_is_missing(self):
        for include, store in [(False, True), (True, False)]:
            with self.subTest(include_models=include, store_available=store):
                for leftover in self.exports.iterdir():
                    leftover.unlink()
                result, manifest = self.build_with_models(set(), include_models=include,
                                                          store_available=store)
                self.assertEqual(manifest['bundled_models'], [])
                self.assertIs(manifest['models_bundled'], False)
                self.assertIs(result['models_bundled'], False)
                self.assertFalse(any(k.startswith('models/') for k in manifest['files']))

    def test_manifest_lists_a_model_shared_by_embedding_and_llm_once(self):
        result, manifest = self.build_with_models(set(), llm_model='embed:v1')
        self.assertEqual(manifest['bundled_models'], ['embed:v1'])
        self.assertIs(manifest['models_bundled'], True)

    def test_manifest_leaves_out_a_model_whose_file_copy_failed(self):
        result, manifest = self.build_with_models(set(), broken_copy={'chat:v2'})
        self.assertEqual(manifest['bundled_models'], ['embed:v1'])
        self.assertIs(manifest['models_bundled'], True)
        self.assertTrue(any(w.startswith("model 'chat:v2' could not be bundled")
                            for w in manifest['warnings']))

    def test_manifest_lists_only_models_actually_bundled(self):
        for failing, expected in [({'chat:v2'}, ['embed:v1']), ({'embed:v1'}, ['chat:v2']),
                                  (set(), ['chat:v2', 'embed:v1']), ({'embed:v1', 'chat:v2'}, [])]:
            with self.subTest(failing=sorted(failing)):
                for leftover in self.exports.iterdir():
                    leftover.unlink()
                result, manifest = self.build_with_models(failing)
                self.assertEqual(manifest['bundled_models'], expected)
                self.assertEqual(manifest['models_bundled'], bool(expected))
                self.assertEqual(result['models_bundled'], bool(expected))
                for ref in ('embed:v1', 'chat:v2'):
                    folder = ref.replace(':', '/')
                    self.assertEqual(f'models/{folder}/manifest.json' in manifest['files'],
                                     ref in expected)
                for model in sorted(failing):
                    self.assertTrue(any(w.startswith(f"model '{model}' could not be bundled")
                                        for w in manifest['warnings']))

    def test_package_readme_contents_come_from_the_partial(self):
        with patch.object(packager, 'read_chunks', return_value=[]), \
             patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': settings.embed_model}), \
             patch.object(packager, '_ingest_config', return_value=None), \
             patch.object(packager, '_goldstandard_sessions', return_value=[]), \
             patch.object(packager.sources, 'load_index', return_value={'documents': {}}), \
             patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}):
            result = packager.build('Corpus')
        with tarfile.open(self.exports / result['filename']) as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith('/README.md'))
            readme = archive.extractfile(member).read().decode()
        partial = (packager._TEMPLATE_DIR / 'partials' / 'contents.md').read_text().rstrip('\n')
        self.assertTrue(readme.rstrip('\n').endswith('## Contents\n\n' + partial))
        self.assertNotIn('@@', readme)

    def test_failure_text_does_not_promise_an_intact_original(self):
        # #385: a replace failure after the delete, or a tune failure during the
        # final replacement, can leave the name missing; the data is in the
        # recovery collection the error names (recovered_as).
        help_page = packager.render_help(1024)
        with patch.object(packager, 'read_chunks', return_value=[]), \
             patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': settings.embed_model}), \
             patch.object(packager, '_ingest_config', return_value=None), \
             patch.object(packager, '_goldstandard_sessions', return_value=[]), \
             patch.object(packager.sources, 'load_index', return_value={'documents': {}}), \
             patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}):
            result = packager.build('Corpus')
        with tarfile.open(self.exports / result['filename']) as archive:
            member = next(m for m in archive.getmembers() if m.name.endswith('/README.md'))
            readme = archive.extractfile(member).read().decode()
        for name, text in (('help page', help_page), ('package README', readme)):
            with self.subTest(name=name):
                self.assertNotIn('leaves the original intact', text)
                self.assertNotIn('leaves it intact', text)
                self.assertNotIn('a failed tune leaves', text)
                self.assertIn('recovery collection', text)
        replace_row = next(line for line in help_page.splitlines() if line.startswith('| `replace`'))
        self.assertIn('recovered_as', replace_row)
        self.assertIn('missing or partial', replace_row)
        tuning = help_page.split('## Tuning after import', 1)[1].split('**Gold-standard', 1)[0]
        self.assertIn('before cutover', tuning)
        self.assertIn('recovered_as', tuning)

    def test_transfer_page_replace_description_names_the_recovery_collection(self):
        root = Path(os.environ.get('RAG_TEST_API_DIR') or '.').resolve().parent  # repo root on the host
        page = root / 'ui' / 'src' / 'pages' / 'TransferPage.tsx'
        if not page.is_file():  # the api container has no ui/ tree
            self.skipTest('ui source not mounted')
        line = next(l for l in page.read_text(encoding='utf-8').splitlines() if "id: 'replace'" in l)
        self.assertNotIn('leaves it intact', line)
        self.assertIn('recovery collection', line)

    def test_export_rejects_invalid_stored_settings_without_publishing_package(self):
        for value in INVALID:
            if value is None:  # None denotes no saved file, not an invalid saved config.
                continue
            with self.subTest(value=value), ExitStack() as mocks:
                chunks = mocks.enter_context(patch.object(packager, 'read_chunks', return_value=[]))
                mocks.enter_context(patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': settings.embed_model}))
                mocks.enter_context(patch.object(packager, '_ingest_config', return_value=None))
                mocks.enter_context(patch.object(retrieval_config, 'load', return_value=value))
                with self.assertRaises(ValueError) as caught:
                    packager.build('Corpus')
                message = str(caught.exception)
                self.assertIn("'Corpus'", message)
                self.assertIn('Retrieval page', message)
                if isinstance(value, dict):
                    self.assertIn(next(iter(value)), message)
                else:
                    self.assertIn('not a JSON object', message)
                chunks.assert_not_called()
                self.assertEqual(list(self.exports.iterdir()), [])

    def test_symlinked_settings_fail_with_package_corrupt(self):
        target = self.root / 'outside.json'
        target.write_text('{}')
        (self.pkg / 'retrieval_config.json').symlink_to(target)
        with self.assertRaises(packager.PackageError) as caught:
            importer._read_retrieval_config(self.pkg, 'Corpus')
        self.assertEqual(caught.exception.code, 'PACKAGE_CORRUPT')
        self.assertEqual(caught.exception.detail, {'file': 'retrieval_config.json'})

    def test_render_refuses_a_template_token_without_a_value(self):
        with self.assertRaises(RuntimeError):
            packager._render('retrieve.py.tmpl', {'COLLECTION_NAME': repr('Corpus')})


class ModelTagFolderTests(unittest.TestCase):
    """Two tags of one model name, and a folder used only for its own tag (#487).

    Uses the real model_bundle export and install against temporary Ollama
    stores holding inert bytes; only Ollama's own listing is stubbed."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / 'store'
        self.target = self.root / 'target'
        (self.target / 'models').mkdir(parents=True)
        self.exports = self.root / 'exports'
        self.exports.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in [('upload_dir', self.root / 'uploads'), ('exports_dir', self.exports),
                           ('sources_dir', self.root / 'sources')]:
            self.stack.enter_context(patch.object(settings, key, str(value)))
        self.stack.enter_context(patch.object(retrieval_config, '_DIR', None))
        self.stack.enter_context(patch.object(importer, '_log'))
        self.manifests = {model: self.put_model(model) for model in ('foo:a', 'foo:b', 'bar:x')}

    def put_model(self, model):
        """An Ollama manifest and blobs with bytes of this model's own."""
        name, tag = model.split(':')
        blobs = self.store / 'models' / 'blobs'
        blobs.mkdir(parents=True, exist_ok=True)
        digests = []
        for part in ('config', 'weights'):
            data = f'{part} of {model}'.encode()
            digest = 'sha256:' + hashlib.sha256(data).hexdigest()
            (blobs / digest.replace(':', '-')).write_bytes(data)
            digests.append(digest)
        manifest = json.dumps({'config': {'digest': digests[0]},
                               'layers': [{'digest': digests[1]}]}).encode()
        path = self.store / 'models' / 'manifests' / model_bundle.REGISTRY / model_bundle.NAMESPACE / name / tag
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(manifest)
        return manifest

    def export(self, embed, llm):
        with ExitStack() as mocks:
            for key, value in [('ollama_models_dir', self.store), ('embed_model', embed), ('llm_model', llm)]:
                mocks.enter_context(patch.object(settings, key, str(value)))
            mocks.enter_context(patch.object(packager, 'read_chunks', return_value=[]))
            mocks.enter_context(patch.object(packager.wc, '_collection_config_sync', return_value={'embedding_model': embed}))
            mocks.enter_context(patch.object(packager, '_ingest_config', return_value=None))
            mocks.enter_context(patch.object(packager, '_goldstandard_sessions', return_value=[]))
            mocks.enter_context(patch.object(packager.sources, 'load_index', return_value={'documents': {}}))
            mocks.enter_context(patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}))
            result = packager.build('Corpus', include_models=True)
        return packager.open_package(self.exports / result['filename'], Path(tempfile.mkdtemp(dir=self.root)))

    def ensure(self, pkg, manifest, embed, llm):
        with patch.object(settings, 'ollama_models_dir', str(self.target)), \
             patch.object(settings, 'embed_model', embed), patch.object(settings, 'llm_model', llm), \
             patch.object(importer, '_ollama_reports', lambda model: True):
            return importer._ensure_models(pkg, manifest)

    def installed(self, model):
        name, tag = model.split(':')
        path = self.target / 'models' / 'manifests' / model_bundle.REGISTRY / model_bundle.NAMESPACE / name / tag
        return path.read_bytes() if path.is_file() else None

    def legacy_package(self, folder_model, manifest_models):
        """A package in the pre-#487 layout: `models/<name>/` holding `folder_model`."""
        pkg = self.root / 'legacy'
        name, tag = folder_model.split(':')
        source = json.loads(self.manifests[folder_model])
        (pkg / 'models' / name / 'blobs').mkdir(parents=True)
        (pkg / 'models' / name / 'manifest.json').write_bytes(self.manifests[folder_model])
        for digest in [source['config']['digest']] + [layer['digest'] for layer in source['layers']]:
            blob = self.store / 'models' / 'blobs' / digest.replace(':', '-')
            (pkg / 'models' / name / 'blobs' / blob.name).write_bytes(blob.read_bytes())
        embed, llm = manifest_models
        return pkg, {'embedding': {'model': embed}, 'llm': {'model': llm}}

    # Each test checks the import outcome first, so on develop it fails on the
    # wrong install itself rather than on the new layout or signature.
    def test_two_tags_of_one_name_are_bundled_apart_and_each_installed_as_itself(self):
        pkg, manifest = self.export('foo:a', 'foo:b')
        notes = self.ensure(pkg, manifest, 'foo:a', 'foo:b')
        self.assertEqual(self.installed('foo:a'), self.manifests['foo:a'])  # develop: foo:b's
        self.assertEqual(self.installed('foo:b'), self.manifests['foo:b'])
        self.assertEqual(sum('installed from the package' in note for note in notes), 2, notes)
        for model in ('foo:a', 'foo:b'):
            folder = 'models/' + model.replace(':', '/')
            self.assertIn(f'{folder}/manifest.json', manifest['files'])
            self.assertEqual((pkg / folder / 'manifest.json').read_bytes(), self.manifests[model])
        self.assertEqual(manifest['bundled_models'], ['foo:a', 'foo:b'])
        self.assertEqual(model_bundle.bundled_models(pkg, manifest), ['foo:a', 'foo:b'])

    def test_a_bundled_tag_is_never_installed_under_another_tag(self):
        pkg, manifest = self.export('foo:a', 'bar:x')
        notes = self.ensure(pkg, manifest, 'foo:a', 'bar:y')
        self.assertEqual(self.installed('foo:a'), self.manifests['foo:a'])
        self.assertIsNone(self.installed('bar:y'))  # develop: bar:x's
        self.assertIn("model 'bar' is absent and not bundled; queries will fail until it is pulled", notes)
        self.assertEqual(manifest['bundled_models'], ['bar:x', 'foo:a'])

    def test_an_old_package_folder_installs_for_the_one_tag_its_manifest_names(self):
        pkg, manifest = self.legacy_package('foo:a', ('foo:a', 'bar:x'))
        notes = self.ensure(pkg, manifest, 'foo:a', 'bar:x')
        self.assertEqual(self.installed('foo:a'), self.manifests['foo:a'])
        self.assertIn("model 'bar' is absent and not bundled; queries will fail until it is pulled", notes)
        self.assertEqual(model_bundle.bundled_models(pkg, manifest), ['foo:a'])

    def test_an_old_package_folder_shared_by_two_tags_counts_as_not_bundled(self):
        # The pre-#487 export wrote both tags into models/foo/, the LLM's last.
        pkg, manifest = self.legacy_package('foo:b', ('foo:a', 'foo:b'))
        with self.assertRaises(packager.PackageError) as caught:  # develop: installs foo:b as foo:a
            self.ensure(pkg, manifest, 'foo:a', 'foo:b')
        self.assertEqual(caught.exception.code, 'EMBEDDING_MODEL_MISSING')
        self.assertIn('does not bundle it', str(caught.exception))
        self.assertIsNone(self.installed('foo:a'))
        self.assertIsNone(self.installed('foo:b'))
        self.assertEqual(model_bundle.bundled_models(pkg, manifest), [])

    def test_an_old_package_folder_is_not_installed_under_another_tag(self):
        pkg, manifest = self.legacy_package('bar:x', ('foo:a', 'bar:x'))
        shutil.copytree(self.store / 'models', self.target / 'models', dirs_exist_ok=True)  # foo:a present
        notes = self.ensure(pkg, manifest, 'foo:a', 'bar:y')
        self.assertIn("model 'foo' already present; left untouched", notes)
        self.assertIsNone(self.installed('bar:y'))  # develop: bar:x's
        self.assertIn("model 'bar' is absent and not bundled; queries will fail until it is pulled", notes)
        self.assertEqual(model_bundle.bundled_models(pkg, manifest), ['bar:x'])

if __name__ == '__main__':
    unittest.main()
