"""Bundled-model byte integrity and publication boundary, using inert bytes."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from services import model_bundle as models

NEEDS_REPOSITORY = 'needs the whole repository mounted (see scripts/verify/README.md)'


def repository_root():
    parents = Path(__file__).resolve().parents
    root = parents[2] if len(parents) > 2 else None
    return root if root is not None and (root / 'IMPLEMENTATION.md').is_file() else None


class BundleFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / 'package'
        self.store = self.root / 'store'
        self.model = 'review-model:latest'
        self.patch = patch.object(settings, 'ollama_models_dir', str(self.store))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.src = self.pkg / 'models' / 'review-model' / 'latest'  # a folder per name and tag (#487)
        (self.src / 'blobs').mkdir(parents=True)
        self.contents = [b'ordinary config bytes', b'ordinary layer bytes']
        self.digests = ['sha256:' + hashlib.sha256(data).hexdigest() for data in self.contents]
        self.manifest = {'config': {'digest': self.digests[0]}, 'layers': [{'digest': self.digests[1]}]}
        self.manifest_file = self.src / 'manifest.json'
        self.manifest_bytes = json.dumps(self.manifest).encode()
        self.manifest_file.write_bytes(self.manifest_bytes)
        for digest, data in zip(self.digests, self.contents):
            (self.src / 'blobs' / digest.replace(':', '-')).write_bytes(data)

    def assert_unpublished(self):
        self.assertFalse(models.manifest_path(self.model).exists())
        self.assertFalse(list(self.store.rglob('*.partial')))


class ModelBundleTests(BundleFixture):
    def test_valid_install_and_existing_content_are_byte_identical_and_unchanged(self):
        models.install_model(self.pkg, self.model)
        self.assertTrue(models.is_installed(self.model))
        self.assertEqual(models.manifest_path(self.model).read_bytes(), self.manifest_bytes)
        stamps = {models.blob_path(d): models.blob_path(d).stat().st_mtime_ns for d in self.digests}
        models.install_model(self.pkg, self.model)
        for digest, content in zip(self.digests, self.contents):
            path = models.blob_path(digest)
            self.assertEqual(path.read_bytes(), content)
            self.assertEqual(path.stat().st_mtime_ns, stamps[path])

    def test_corrupt_package_is_refused_even_when_shared_target_blob_is_healthy(self):
        target = models.blob_path(self.digests[1]); target.parent.mkdir(parents=True)
        target.write_bytes(self.contents[1]); stamp = target.stat().st_mtime_ns
        (self.src / 'blobs' / target.name).write_bytes(b'different inert bytes')
        with self.assertRaisesRegex(ValueError, 'disagree'):
            models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertEqual(target.read_bytes(), self.contents[1])
        self.assertEqual(target.stat().st_mtime_ns, stamp)
        self.assertFalse(models.blob_path(self.digests[0]).exists())

    def test_corrupt_existing_blob_is_not_overwritten_or_activated(self):
        target = models.blob_path(self.digests[1]); target.parent.mkdir(parents=True)
        target.write_bytes(b'corrupt existing inert bytes')
        with self.assertRaisesRegex(ValueError, 'restore'):
            models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertEqual(target.read_bytes(), b'corrupt existing inert bytes')

    def test_is_installed_requires_actual_bytes_not_filenames(self):
        models.install_model(self.pkg, self.model)
        models.blob_path(self.digests[1]).write_bytes(b'different bytes')
        self.assertFalse(models.is_installed(self.model))

    def test_invalid_reference_grammar_and_missing_references_are_refused(self):
        for invalid in ('sha256:abc', 'sha512:' + 'a'*64, 'sha256:' + 'A'*64, None, 42):
            with self.subTest(reference=invalid):
                bad = {'config': {'digest': self.digests[0]}, 'layers': [{'digest': invalid}]}
                self.manifest_file.write_text(json.dumps(bad))
                with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
                self.assert_unpublished()
        for invalid in ({}, {'config': [], 'layers': []}, {'config': {'digest': self.digests[0]}, 'layers': [None]}):
            self.manifest_file.write_text(json.dumps(invalid))
            with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
            self.assert_unpublished()

    def test_missing_package_blob_does_not_publish_anything(self):
        (self.src / 'blobs' / self.digests[1].replace(':', '-')).unlink()
        with self.assertRaises(FileNotFoundError): models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertFalse(models.blob_path(self.digests[0]).exists())

    def test_store_and_package_symlinks_are_refused(self):
        outside = self.root / 'outside'; outside.mkdir()
        for source_side in (True, False):
            with self.subTest(package=source_side):
                if source_side:
                    location = self.src / 'blobs' / self.digests[1].replace(':', '-')
                    location.unlink(); destination = outside / 'layer'; destination.write_bytes(self.contents[1])
                else:
                    location = self.store / 'models' / 'blobs'; location.parent.mkdir(parents=True)
                    destination = outside
                location.symlink_to(destination)
                with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
                self.assertFalse(list(outside.glob('sha256-*')))
                location.unlink()
                if source_side: location.write_bytes(self.contents[1])
        self.assert_unpublished()

    def test_unsafe_model_components_are_refused(self):
        for name in ('review/other', 'review:../tag', '../review', 'review:tag:other'):
            with self.subTest(model=name), self.assertRaises(ValueError): models.install_model(self.pkg, name)
        self.assert_unpublished()

    def test_changed_source_during_copy_cannot_publish_manifest_or_partial_blob(self):
        actual = models._publish
        def change_then_write(path, write, **kwargs):
            if path.name == self.digests[1].replace(':', '-'):
                (self.src / 'blobs' / path.name).write_bytes(b'changed inert bytes')
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=change_then_write):
            with self.assertRaisesRegex(ValueError, 'changed'): models.install_model(self.pkg, self.model)
        self.assert_unpublished()
        self.assertFalse(models.blob_path(self.digests[1]).exists())

    def test_concurrent_valid_blob_publication_is_reused_without_replacement(self):
        actual = models._publish
        def winning_writer(path, write, **kwargs):
            if path.name == self.digests[1].replace(':', '-'):
                path.write_bytes(self.contents[1]); stamp = path.stat().st_mtime_ns
                result = actual(path, write, **kwargs)
                self.assertEqual(path.stat().st_mtime_ns, stamp)
                return result
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=winning_writer): models.install_model(self.pkg, self.model)
        self.assertTrue(models.is_installed(self.model))

    def test_concurrent_corrupt_blob_publication_is_refused(self):
        actual = models._publish
        def winning_writer(path, write, **kwargs):
            if path.name == self.digests[1].replace(':', '-'): path.write_bytes(b'wrong bytes')
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=winning_writer):
            with self.assertRaises(ValueError): models.install_model(self.pkg, self.model)
        self.assert_unpublished()

    def test_captured_manifest_is_published_even_if_package_manifest_changes(self):
        actual = models._publish
        def changing_manifest(path, write, **kwargs):
            self.manifest_file.write_text('{}')
            return actual(path, write, **kwargs)
        with patch.object(models, '_publish', side_effect=changing_manifest): models.install_model(self.pkg, self.model)
        self.assertEqual(models.manifest_path(self.model).read_bytes(), self.manifest_bytes)
        self.assertTrue(models.is_installed(self.model))

    def test_large_blob_is_read_in_bounded_blocks(self):
        data = b'plain inert bytes' * 150000
        digest = 'sha256:' + hashlib.sha256(data).hexdigest()
        self.manifest['layers'] = [{'digest': digest}]
        self.manifest_file.write_text(json.dumps(self.manifest))
        (self.src / 'blobs' / digest.replace(':', '-')).write_bytes(data)
        actual = Path.open
        reads = []
        test = self
        class Reader:
            def __init__(self, file): self.file = file
            def __enter__(self): self.file.__enter__(); return self
            def __exit__(self, *args): return self.file.__exit__(*args)
            def read(self, size=-1):
                test.assertGreater(size, 0)
                test.assertLessEqual(size, 1024 * 1024)
                reads.append(size)
                return self.file.read(size)
        def bounded(path, *args, **kwargs):
            file = actual(path, *args, **kwargs)
            return Reader(file) if path.name.startswith('sha256-') else file
        with patch.object(Path, 'open', bounded): models.install_model(self.pkg, self.model)
        self.assertGreater(len(reads), 6)
        self.assertEqual(models.blob_path(digest).stat().st_size, len(data))

    def test_manifest_publication_failure_preserves_previous_manifest(self):
        destination = models.manifest_path(self.model); destination.parent.mkdir(parents=True)
        prior = json.dumps({**self.manifest, 'schemaVersion': 2}).encode()
        destination.write_bytes(prior)
        actual = Path.replace
        def fail_manifest(path, target):
            if target == destination: raise OSError('publication unavailable')
            return actual(path, target)
        with patch.object(Path, 'replace', fail_manifest):
            with self.assertRaises(OSError): models.install_model(self.pkg, self.model)
        self.assertEqual(destination.read_bytes(), prior)
        self.assertFalse(list(self.store.rglob('*.partial')))

    def test_import_already_present_model_leaves_manifest_and_shared_blobs_untouched(self):
        from services import importer
        models.install_model(self.pkg, self.model)
        paths = [models.manifest_path(self.model), *(models.blob_path(d) for d in self.digests)]
        stamps = {path: path.stat().st_mtime_ns for path in paths}
        with patch.object(settings, 'embed_model', self.model), patch.object(settings, 'llm_model', self.model):
            notes = importer._ensure_models(self.pkg, {'embedding': {'model': self.model}})
        self.assertTrue(all('already present' in note for note in notes))
        self.assertEqual({path: path.stat().st_mtime_ns for path in paths}, stamps)


class ImporterModelIntegrityTests(BundleFixture):
    """The import route's handling of the new refusals (reviewer-added)."""

    def ensure(self, llm_model=None, embed_model=None, ollama_reports=False):
        from services import importer
        (self.store / 'models').mkdir(parents=True, exist_ok=True)  # else: 'store is not mounted'
        embed = embed_model or self.model
        with patch.object(settings, 'embed_model', embed), \
             patch.object(settings, 'llm_model', llm_model or self.model), \
             patch.object(importer, '_ollama_reports',
                          ollama_reports if callable(ollama_reports) else lambda model: ollama_reports):
            return importer._ensure_models(self.pkg, {'embedding': {'model': embed}})

    def install_then_corrupt(self):
        models.install_model(self.pkg, self.model)
        models.blob_path(self.digests[1]).write_bytes(b'corrupt installed inert bytes')

    def test_corrupt_bundled_embedding_model_is_refused_as_missing_without_publishing(self):
        from services.packager import PackageError
        (self.src / 'blobs' / self.digests[1].replace(':', '-')).write_bytes(b'different inert bytes')
        with self.assertRaises(PackageError) as caught: self.ensure()
        self.assertEqual(caught.exception.code, 'EMBEDDING_MODEL_MISSING')
        self.assertIn('disagree', str(caught.exception))
        self.assert_unpublished()

    def test_corrupt_existing_shared_blob_makes_import_report_restore(self):
        from services.packager import PackageError
        target = models.blob_path(self.digests[1]); target.parent.mkdir(parents=True)
        target.write_bytes(b'corrupt existing inert bytes')
        with self.assertRaises(PackageError) as caught: self.ensure()
        self.assertEqual(caught.exception.code, 'EMBEDDING_MODEL_MISSING')
        self.assertIn('restore', str(caught.exception))
        self.assertEqual(target.read_bytes(), b'corrupt existing inert bytes')
        self.assert_unpublished()

    def test_valid_bundled_embedding_model_is_installed_through_the_importer(self):
        notes = self.ensure(ollama_reports=True)  # the install is confirmed through Ollama (#390)
        self.assertTrue(any('installed from the package' in note for note in notes), notes)
        self.assertTrue(models.is_installed(self.model))

    # A namespaced model ('user/model') has no path in the store layout, so the
    # store can't check or install it; import defers to Ollama instead (#81).
    def test_namespaced_optional_llm_does_not_break_import(self):
        notes = self.ensure(llm_model='someone/assistant:latest',
                            ollama_reports=lambda model: model == self.model)
        self.assertTrue(any("'someone/assistant:latest' is absent" in note for note in notes), notes)

    def test_namespaced_embedding_model_reported_by_ollama_is_accepted(self):
        notes = self.ensure(embed_model='someone/embedder', ollama_reports=True)
        self.assertTrue(any("'someone/embedder' reported by Ollama" in note for note in notes), notes)

    def test_namespaced_embedding_model_absent_is_missing_with_a_pull_hint(self):
        from services.packager import PackageError
        with self.assertRaises(PackageError) as caught:
            self.ensure(embed_model='someone/embedder', ollama_reports=False)
        self.assertEqual(caught.exception.code, 'EMBEDDING_MODEL_MISSING')
        self.assertIn('namespaced', str(caught.exception))
        self.assertIn('ollama pull someone/embedder', str(caught.exception))

    # A present model whose bytes disagree with its digests is not "missing":
    # telling the user to pull it would send them the wrong way (#81).
    def test_present_but_corrupt_embedding_model_fails_integrity_not_missing(self):
        from services.packager import PackageError
        self.install_then_corrupt()
        self.assertEqual(models.installed_state(self.model), 'corrupt')
        with self.assertRaises(PackageError) as caught: self.ensure()
        self.assertEqual(caught.exception.code, 'MODEL_INTEGRITY_FAILED')
        self.assertIn("don't match their checksums", str(caught.exception))
        self.assertEqual(models.blob_path(self.digests[1]).read_bytes(), b'corrupt installed inert bytes')

    def test_present_but_corrupt_optional_llm_is_a_note(self):
        from services import importer
        self.install_then_corrupt()
        good = 'good-embedder:latest'
        # Only the LLM is corrupt; the embedding model is reported present.
        real_state = models.installed_state
        state = lambda model: 'present' if model == good else real_state(model)
        (self.store / 'models').mkdir(parents=True, exist_ok=True)
        with patch.object(importer.model_bundle, 'installed_state', state), \
             patch.object(settings, 'embed_model', good), patch.object(settings, 'llm_model', self.model):
            notes = importer._ensure_models(self.pkg, {'embedding': {'model': good}})
        self.assertTrue(any("don't match their checksums" in note for note in notes), notes)

    def test_installed_state_distinguishes_absent_present_and_corrupt(self):
        self.assertEqual(models.installed_state(self.model), 'absent')
        models.install_model(self.pkg, self.model)
        self.assertEqual(models.installed_state(self.model), 'present')
        models.blob_path(self.digests[0]).unlink()
        self.assertEqual(models.installed_state(self.model), 'corrupt')
        self.assertFalse(models.is_installed(self.model))
        self.assertFalse(models.supports_name('someone/model'))
        self.assertTrue(models.supports_name('phi3.5:3.8b'))

    # The two #81 scenarios exactly as the issue states them, patching only what
    # exists on develop too, so each fails there for the reported reason.
    def unbundled_package(self):
        pkg = self.root / 'unbundled'
        pkg.mkdir()
        return pkg

    def ensure_real(self, pkg, llm_model, tags=frozenset(), tags_error=None):
        from services import importer, ollama_client
        (self.store / 'models').mkdir(parents=True, exist_ok=True)

        async def list_models():
            if tags_error:
                raise tags_error
            return set(tags)
        with patch.object(settings, 'embed_model', self.model), \
             patch.object(settings, 'llm_model', llm_model), \
             patch.object(ollama_client, 'list_models', list_models, create=True):
            return importer._ensure_models(pkg, {'embedding': {'model': self.model}})

    def test_namespaced_llm_with_unbundled_package_completes_with_a_note(self):
        models.install_model(self.pkg, self.model)
        notes = self.ensure_real(self.unbundled_package(), 'someone/assistant')
        self.assertTrue(any("'someone/assistant' is absent" in note for note in notes), notes)
        self.assertTrue(any('already present' in note for note in notes), notes)

    def test_corrupt_installed_embedding_with_unbundled_package_is_integrity_failure(self):
        from services.packager import PackageError
        self.install_then_corrupt()
        manifest = models.manifest_path(self.model).read_bytes()
        with self.assertRaises(PackageError) as caught:
            self.ensure_real(self.unbundled_package(), self.model)
        self.assertEqual(caught.exception.code, 'MODEL_INTEGRITY_FAILED')
        self.assertIn('re-pull', str(caught.exception))
        self.assertEqual(caught.exception.detail, {'model': 'review-model'})
        self.assertEqual(models.manifest_path(self.model).read_bytes(), manifest)
        self.assertEqual(models.blob_path(self.digests[1]).read_bytes(), b'corrupt installed inert bytes')

    def test_ollama_reports_defaults_the_tag_and_matches_exactly(self):
        from services import importer, ollama_client
        tags = {'someone/embedder:latest', 'hf.co/org/model:Q4_K_M', 'registry:5000/team/m:latest'}

        async def list_models():
            return tags
        with patch.object(ollama_client, 'list_models', list_models):
            self.assertTrue(importer._ollama_reports('someone/embedder'))
            self.assertTrue(importer._ollama_reports('someone/embedder:latest'))
            self.assertTrue(importer._ollama_reports('hf.co/org/model:Q4_K_M'))
            self.assertTrue(importer._ollama_reports('registry:5000/team/m'))
            self.assertFalse(importer._ollama_reports('someone/embedder:v2'))
            self.assertFalse(importer._ollama_reports('hf.co/org/model'))

    def test_namespaced_embedding_when_ollama_is_unreachable_says_so(self):
        from services.packager import PackageError
        from services import importer
        models.install_model(self.pkg, self.model)
        with self.assertRaises(PackageError) as caught, \
             patch.object(settings, 'embed_model', 'someone/embedder'), \
             patch.object(settings, 'llm_model', self.model):
            from services import ollama_client

            async def down():
                raise OSError('connection refused')
            (self.store / 'models').mkdir(parents=True, exist_ok=True)
            with patch.object(ollama_client, 'list_models', down):
                importer._ensure_models(self.unbundled_package(), {'embedding': {'model': 'someone/embedder'}})
        # Not EMBEDDING_MODEL_MISSING: the model may well be there (#85).
        self.assertEqual(caught.exception.code, 'IMPORT_FAILED')
        self.assertIn("Couldn't reach Ollama", str(caught.exception))
        self.assertIn('someone/embedder', str(caught.exception))

    def test_symlinked_manifest_path_is_corrupt_not_an_import_error(self):
        models.install_model(self.pkg, self.model)
        mp = models.manifest_path(self.model)
        elsewhere = self.root / 'elsewhere'
        mp.rename(elsewhere)
        mp.symlink_to(elsewhere)
        self.assertEqual(models.installed_state(self.model), 'corrupt')
        self.assertFalse(models.is_installed(self.model))

    def test_unreadable_installed_manifest_is_corrupt_not_absent(self):
        models.install_model(self.pkg, self.model)
        models.manifest_path(self.model).write_text('{not json')
        self.assertEqual(models.installed_state(self.model), 'corrupt')
        self.assertFalse(models.is_installed(self.model))


class InterruptedPublicationTests(BundleFixture):
    """A killed installer must not leave copies accumulating in the live blob store."""

    def install_and_kill_before_link(self):
        script = (
            'import os, signal, sys\n'
            'from pathlib import Path\n'
            'from unittest.mock import patch\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'from config import settings\n'
            'from services import model_bundle as models\n'
            'def die(*args):\n'
            '    os.kill(os.getpid(), signal.SIGKILL)  # the blob copy is written; stop before it is published\n'
            'with patch.object(settings, "ollama_models_dir", sys.argv[2]), \\\n'
            '     patch.object(os, "link", die), patch.object(Path, "replace", die):\n'
            '    models.install_model(Path(sys.argv[3]), sys.argv[4])\n')
        api = os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api')
        import subprocess
        result = subprocess.run([sys.executable, '-c', script, api, str(self.store), str(self.pkg), self.model])
        self.assertEqual(result.returncode, -9)

    def test_killed_install_is_not_activated(self):
        self.install_and_kill_before_link()
        self.assertFalse(models.manifest_path(self.model).exists())
        self.assertFalse(models.is_installed(self.model))

    # Security review Medium: each interrupted attempt leaves a uniquely named,
    # full-size '<blob>.<random>.partial' in the live blobs folder, and nothing
    # sweeps it. On develop a retry reused and replaced the one fixed-name
    # '.partial'. Remove the decorator once stale partials are swept.
    @unittest.expectedFailure
    def test_retry_after_killed_install_leaves_no_partial_copies(self):
        self.install_and_kill_before_link()
        self.install_and_kill_before_link()
        models.install_model(self.pkg, self.model)
        self.assertTrue(models.is_installed(self.model))
        self.assertEqual([p.name for p in self.store.rglob('*.partial')], [])


class ImplementationTests(unittest.TestCase):
    def test_embedded_model_source_matches_runtime(self):
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        text = (root / 'IMPLEMENTATION.md').read_text()
        for name, fence, language in [('api/services/model_bundle.py', '```', 'python'),
                                      ('scripts/verify/model_integrity.py', '```', 'python'),
                                      ('scripts/verify/README.md', '````', 'markdown')]:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = text.index(header) + len(header)
                end = text.index('\n' + fence + '\n', start)
                self.assertEqual(text[start:end], (root / name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
