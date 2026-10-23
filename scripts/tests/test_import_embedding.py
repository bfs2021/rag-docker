"""Import check 4 (E12) and the post-install model check, with the real code (#390).

Only Ollama is replaced: the embed call that learns this instance's vector width,
and the model list that says whether Ollama reports a model.
"""
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from services import importer, model_bundle, ollama_client, packager
from services.packager import PackageError

INSTANCE = 'instance-embedder'
OTHER = 'other-embedder'
PROMISE = 're-embedded after import'


def embedding_width(width):
    """Ollama's embed call returning a vector of `width`, or failing when None."""
    async def embed(text):
        if width is None:
            raise ConnectionError('ollama unreachable')
        return [0.0] * width
    return embed


def model_list(names=None, error=None):
    async def list_models():
        if error:
            raise error
        return set(names or ())
    return list_models


class MismatchTests(unittest.TestCase):
    """E12: the import refuses a package embedded with another model."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.exports = self.root / 'exports'
        self.uploads = self.root / 'uploads'
        self.exports.mkdir()
        self.uploads.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in [('upload_dir', self.uploads), ('exports_dir', self.exports),
                           ('sources_dir', self.root / 'sources'), ('embed_model', INSTANCE)]:
            self.stack.enter_context(patch.object(settings, key, str(value)))
        self.stack.enter_context(patch.object(importer, '_probed_dimensions', None))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.object(importer, '_log'))

    def archive(self, model, dimensions, fidelity):
        """A digest-valid package, so checks 1-3a pass and check 4 decides."""
        pkg = self.root / f'package-{fidelity}'
        pkg.mkdir()
        (pkg / 'chunks.jsonl').write_text('')
        if fidelity == 'with-sources':
            content = b'retained source'
            digest = hashlib.sha256(content).hexdigest()
            (pkg / 'sources').mkdir()
            (pkg / 'sources' / digest).write_bytes(content)
            (pkg / 'sources' / 'index.json').write_text(json.dumps(
                {'version': 1, 'documents': {digest: {'filenames': ['a.txt'], 'size': len(content)}}}))
        manifest = {'package_format': 1, 'fidelity': fidelity,
                    'collection': {'name': 'Corpus', 'chunk_count': 0},
                    'embedding': {'model': model, 'dimensions': dimensions},
                    'files': {p.relative_to(pkg).as_posix(): 'sha256:' + packager.sha256_file(p)
                              for p in sorted(pkg.rglob('*')) if p.is_file()}}
        (pkg / 'manifest.json').write_text(json.dumps(manifest))
        name = f'{fidelity}.tar.gz'
        with tarfile.open(self.exports / name, 'w:gz') as tar:
            tar.add(pkg, arcname='package')
        return name

    def run_import(self, filename, width):
        """Run the import job; every step after check 4 is a mock that must stay unused."""
        with ExitStack() as mocks:
            mocks.enter_context(patch.object(ollama_client, 'embed', embedding_width(width)))
            later = [mocks.enter_context(patch.object(module, name)) for module, name in [
                (importer, '_ensure_models'), (importer, '_build'),
                (importer.wc, '_collection_exists_sync'), (importer.wc, '_delete_collection_sync'),
                (importer.wc, 'get_client'), (importer, '_restore_sidecars'),
                (importer, '_mark_started'), (importer.collection_recovery, 'begin')]]
            importer._jobs['test'] = {'status': 'queued'}
            importer._run('test', filename, 'rename')
            for step in later:
                step.assert_not_called()
        return importer._jobs['test']

    def test_e12_names_both_models_and_both_dimension_counts(self):
        for fidelity in ('with-sources', 'chunks-only'):
            with self.subTest(fidelity=fidelity):
                job = self.run_import(self.archive(OTHER, 1024, fidelity), 768)
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(job['error_code'], 'EMBEDDING_MISMATCH')
                message = job['error']
                self.assertNotIn(PROMISE, message)
                self.assertNotIn('once that is supported', message)
                self.assertIn(f"'{OTHER}' (1024 dimensions)", message)
                self.assertIn(f"'{INSTANCE}' (768 dimensions)", message)
                self.assertEqual(job['error_detail'], {
                    'package_model': OTHER, 'package_dimensions': 1024,
                    'instance_model': INSTANCE, 'instance_dimensions': 768})
                self.assertTrue(message.endswith(
                    f"Use an instance running '{OTHER}', or re-export from one."), message)
                if fidelity == 'chunks-only':
                    self.assertIn('cannot be re-embedded from the original documents', message)
                else:
                    self.assertNotIn('re-embedded', message)

    def test_e12_refuses_when_this_instance_width_cannot_be_read(self):
        job = self.run_import(self.archive(OTHER, 1024, 'with-sources'), None)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'EMBEDDING_MISMATCH')
        self.assertIn('instance_dimensions', job['error_detail'])
        self.assertIsNone(job['error_detail']['instance_dimensions'])
        self.assertIn(f"'{INSTANCE}' (whose dimension count couldn't be read from Ollama)",
                      job['error'])
        self.assertNotIn(PROMISE, job['error'])

    def test_same_model_with_a_different_width_is_refused_with_both_counts(self):
        job = self.run_import(self.archive(INSTANCE, 1024, 'chunks-only'), 768)
        self.assertEqual(job['error_code'], 'EMBEDDING_MISMATCH')
        self.assertEqual(job['error_detail'], {
            'package_model': INSTANCE, 'package_dimensions': 1024,
            'instance_model': INSTANCE, 'instance_dimensions': 768})
        self.assertIn('1024', job['error'])
        self.assertIn('768', job['error'])

    def test_same_model_and_width_passes_check_4(self):
        with patch.object(ollama_client, 'embed', embedding_width(768)):
            self.assertIsNone(importer._check_embedding(
                {'fidelity': 'chunks-only', 'embedding': {'model': INSTANCE, 'dimensions': 768}}))


class PostInstallTests(unittest.TestCase):
    """RAG_EXPORT_SPECIFICATIONS.md §6.3: a bundled model is confirmed through Ollama."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = self.root / 'store'
        (self.store / 'models').mkdir(parents=True)
        self.pkg = self.root / 'package'
        self.model = 'review-model:latest'
        src = self.pkg / 'models' / 'review-model' / 'latest'  # a folder per name and tag (#487)
        (src / 'blobs').mkdir(parents=True)
        contents = [b'ordinary config bytes', b'ordinary layer bytes']
        digests = ['sha256:' + hashlib.sha256(data).hexdigest() for data in contents]
        (src / 'manifest.json').write_text(json.dumps(
            {'config': {'digest': digests[0]}, 'layers': [{'digest': digests[1]}]}))
        for digest, data in zip(digests, contents):
            (src / 'blobs' / digest.replace(':', '-')).write_bytes(data)
        patcher = patch.object(settings, 'ollama_models_dir', str(self.store))
        patcher.start()
        self.addCleanup(patcher.stop)

    def ensure(self, list_models, embed=None, llm=None):
        embed = embed or self.model
        with patch.object(settings, 'embed_model', embed), \
             patch.object(settings, 'llm_model', llm or self.model), \
             patch.object(ollama_client, 'list_models', list_models):
            return importer._ensure_models(self.pkg, {'embedding': {'model': embed}})

    def test_installed_model_that_ollama_reports_completes(self):
        notes = self.ensure(model_list({self.model}))
        self.assertIn("model 'review-model' installed from the package", notes)
        self.assertTrue(model_bundle.is_installed(self.model))

    def test_installed_embedding_model_that_ollama_does_not_report_is_missing(self):
        with self.assertRaises(PackageError) as caught:
            self.ensure(model_list({'something-else:latest'}))
        self.assertEqual(caught.exception.code, 'EMBEDDING_MODEL_MISSING')
        self.assertEqual(caught.exception.message,
                         "Installed 'review-model' from the package but Ollama does not report it.")
        self.assertEqual(caught.exception.detail, {'model': 'review-model'})

    def test_installed_model_when_ollama_is_unreachable_says_so(self):
        with self.assertRaises(PackageError) as caught:
            self.ensure(model_list(error=ConnectionError('ollama unreachable')))
        self.assertEqual(caught.exception.code, 'IMPORT_FAILED')
        self.assertIn("couldn't reach Ollama", caught.exception.message)
        self.assertIn('ollama service is running', caught.exception.message)
        self.assertNotIn('does not report', caught.exception.message)
        self.assertEqual(caught.exception.detail, {'model': 'review-model'})

    def test_installed_llm_that_ollama_does_not_report_fails_the_import(self):
        present = 'present-embedder:latest'
        real_state = model_bundle.installed_state
        state = lambda model: 'present' if model == present else real_state(model)
        with patch.object(importer.model_bundle, 'installed_state', state), \
             self.assertRaises(PackageError) as caught:
            self.ensure(model_list({present}), embed=present)
        self.assertEqual(caught.exception.code, 'IMPORT_FAILED')
        self.assertIn('Ollama does not report it', caught.exception.message)
        self.assertEqual(caught.exception.detail, {'model': 'review-model'})


if __name__ == '__main__':
    unittest.main()
