"""Overlap window invariants on inert synthetic text and the ingest worker."""
import os
import hashlib
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from services import chunker, ingest_pipeline, tuning

NEEDS_REPOSITORY = 'needs the whole repository mounted (see scripts/verify/README.md)'


def repository_root():
    parents = Path(__file__).resolve().parents
    root = parents[2] if len(parents) > 2 else None
    return root if root is not None and (root / 'IMPLEMENTATION.md').is_file() else None


def recover(chunks, overlap):
    return chunks[0] + ''.join(c[overlap:] for c in chunks[1:]) if chunks else ''


class OverlapTests(unittest.TestCase):
    def check_windows(self, text, size=1000, overlap=200, minimum=100):
        chunks = chunker.chunk_overlap(text, size, overlap, minimum)
        self.assertEqual(recover(chunks, overlap), text)
        for left, right in zip(chunks, chunks[1:]):
            if overlap:
                self.assertEqual(left[-overlap:], right[:overlap])
        self.assertTrue(all(len(c) <= size for c in chunks[:-1]))
        # One short final window can extend its predecessor by its new suffix.
        bound = size + max(0, min(size, minimum - 1) - overlap)
        self.assertTrue(all(len(c) <= bound for c in chunks))
        return chunks

    def test_default_strategy_boundary_at_the_per_file_window_cap(self):
        # At the project's own defaults (chunk_size=1000, chunk_overlap=200),
        # the 10,000-window cap is reached at exactly 8,000,200 characters:
        # windows = 1 + ceil((L - 1000) / 800). A file this size or smaller
        # ingested successfully before this PR (CharacterTextSplitter had no
        # size cap); one character over now fails every default-strategy
        # ingest of it, not just paragraph-less ones. See chunker.py's
        # MAX_OVERLAP_WINDOWS and the coding review's Medium finding on this
        # regression.
        boundary = 8_000_200
        chunks = chunker.chunk_overlap('x' * boundary, 1000, 200, 100)
        self.assertEqual(len(chunks), chunker.MAX_OVERLAP_WINDOWS)
        with self.assertRaisesRegex(ValueError, 'per-file limit'):
            chunker.chunk_overlap('x' * (boundary + 1), 1000, 200, 100)

    def test_internal_whitespace_run_can_produce_a_blank_stored_window(self):
        # SPECIFICATIONS.md documents that "blank-only input yields no chunks"
        # but says nothing about a single window inside otherwise-nonblank
        # text. The old splitter stripped and dropped blank chunks
        # (`_enforce_min_chunk_size` filters `c.strip()`); raw character
        # windows keep every slice, so a long enough internal whitespace run
        # is stored as a chunk that is entirely blank. This pins down and
        # documents that behavior change rather than leaving it implicit.
        text = 'a' * 50 + ' ' * 2000 + 'b' * 50
        chunks = self.check_windows(text, size=200, overlap=20, minimum=50)
        self.assertTrue(any(c.strip() == '' for c in chunks),
                         'expected at least one whitespace-only window; '
                         'chunk lengths were ' + str([len(c) for c in chunks]))

    def test_tuning_rechunk_over_budget_fails_before_rebuild(self):
        # The coding review noted (by inspection, not a test) that
        # tuning._chunks_from_sources raises before _rebuild is called, so a
        # re-chunk that now hits chunk_overlap's per-file limit fails the
        # tuning job as TUNE_FAILED without touching the live collection.
        # This exercises that path instead of just trusting the inspection.
        with tempfile.TemporaryDirectory() as directory:
            src_dir = Path(directory)
            digest = hashlib.sha256(b'irrelevant retained bytes').hexdigest()
            (src_dir / digest).write_bytes(b'irrelevant retained bytes')
            job_id = 'tune-budget-test'
            tuning._jobs[job_id] = {'job_id': job_id}
            params = {'chunking': {'strategy': 'overlap', 'chunk_size': 1000,
                                    'chunk_overlap': 999, 'similarity_threshold': 0.85,
                                    'min_chunk_size': 100}}
            with patch.object(tuning.settings, 'upload_dir', directory), \
                 patch.object(tuning.sources, 'has_sources', return_value=True), \
                 patch.object(tuning.sources, 'load_index',
                               return_value={'documents': {digest: {'filenames': ['big.txt']}}}), \
                 patch.object(tuning.sources, 'collection_dir', return_value=src_dir), \
                 patch.object(tuning, '_parse_file', return_value=('x' * 100000, [])), \
                 patch.object(tuning, '_rebuild') as rebuild, \
                 patch.object(tuning.wc, 'get_client') as get_client:
                get_client.return_value.collections.get.return_value.iterator.return_value = []
                tuning._run(job_id, 'ReviewTuneBudget', 'rechunk', params)
            job = tuning._jobs.pop(job_id)
            self.assertEqual(job['status'], 'failed', job)
            self.assertEqual(job.get('error_code'), 'TUNE_FAILED', job)
            self.assertIn('per-file limit', job.get('error', ''), job)
            rebuild.assert_not_called()
            # Coverage now reads the existing collection; destructive rebuild is still unreachable.


    def test_long_token_has_bounded_windows_and_exact_coverage(self):
        chunks = self.check_windows('x' * 10000)
        self.assertEqual(len(chunks), 13)
        self.assertEqual(len(chunks[-1]), 400)

    def test_parser_single_newlines_preserve_order_and_overlap(self):
        self.check_windows('\n'.join('synthetic line ' + str(i) for i in range(600)))

    def test_paragraph_separators_and_unicode_are_preserved(self):
        self.check_windows('\n\n'.join('Paragraph ' + str(i) + ' café 🌿 ' * 80 for i in range(30)))

    def test_short_tail_merges_only_new_suffix_with_documented_bound(self):
        chunks = self.check_windows(''.join(chr(65 + i % 26) for i in range(1020)), overlap=10)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 1020)

    def test_zero_overlap_tail_preserves_exact_text_without_added_separator(self):
        self.assertEqual(len(self.check_windows('x' * 1040, overlap=0)[0]), 1040)

    def test_minimum_larger_than_split_target_keeps_the_tail_policy_bounded(self):
        chunks = self.check_windows('x'*10000,minimum=2000)
        self.assertEqual(len(chunks[-1]),1200)
        self.assertEqual(len(chunks),12)

    def test_tail_at_minimum_remains_separate(self):
        chunks = self.check_windows('x' * 1080, overlap=20)
        self.assertEqual([len(c) for c in chunks], [1000, 100])

    def test_document_shorter_than_minimum_is_not_padded(self):
        self.assertEqual(self.check_windows('short'), ['short'])

    def test_exact_window_and_near_total_overlap_do_not_emit_redundant_tail(self):
        self.assertEqual(self.check_windows('x' * 1000), ['x' * 1000])
        self.assertEqual([len(c) for c in self.check_windows('x' * 1001, overlap=999)], [1000, 1000])

    def test_empty_or_whitespace_only_input_has_no_chunks(self):
        for text in ('', ' \n\t '):
            self.assertEqual(chunker.chunk_overlap(text, 1000, 200, 100), [])

    def test_internal_and_boundary_whitespace_is_preserved(self):
        self.check_windows('   a' + ' ' * 2500 + 'b\n\t')

    def test_invalid_sizes_fail_before_splitting(self):
        for size, overlap, minimum in ((0,0,0),(-1,0,0),(10,-1,0),(10,10,0),
                                      (10,11,0),(10,0,-1),(True,0,0),
                                      (10,False,0),(10,0,True),(10.5,0,0)):
            with self.subTest(values=(size,overlap,minimum)), self.assertRaises(ValueError):
                chunker.chunk_overlap('inert',size,overlap,minimum)

    def test_window_limit_accepts_boundary_and_rejects_before_slicing(self):
        count=chunker.MAX_OVERLAP_WINDOWS
        self.assertEqual(len(chunker.chunk_overlap('x'*(10+count-1),10,9,0)),count)
        class NoSlices(str):
            def __getitem__(self,key):raise AssertionError('window allocated before rejection')
        with self.assertRaisesRegex(ValueError,'per-file limit'):
            chunker.chunk_overlap(NoSlices('x'*(10+count)),10,9,0)

    def test_repeated_payload_limit_has_an_independent_boundary(self):
        with patch.object(chunker,'MAX_OVERLAP_OUTPUT_CHARACTERS',100):
            chunks=chunker.chunk_overlap('x'*55,10,5,0)
            self.assertEqual(sum(map(len,chunks)),100)
            with self.assertRaisesRegex(ValueError,'characters'):
                chunker.chunk_overlap('x'*56,10,5,0)

    def test_over_budget_file_fails_before_storage_and_other_file_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);bad=root/'over-budget.txt';bad.write_text('inert')
            good=root/'accepted.txt';good.write_text('inert')
            job_id='overlap-budget-test'
            job={'status':'queued','files_total':2,'files_completed':0,'files_failed':0,'chunks_stored':0,'errors':[]}
            ingest_pipeline._jobs[job_id]=job
            try:
                with patch.object(ingest_pipeline,'_parse_file',side_effect=[('x'*100000,[]),('inert text',[])]), \
                     patch.object(ingest_pipeline.wc,'_insert_chunks_sync') as store, \
                     patch.object(ingest_pipeline.wc, '_source_has_chunks_sync', return_value=False), \
                     patch.object(ingest_pipeline.sources,'store') as retained:
                    ingest_pipeline._process_job_sync(job_id,[bad,good],root,'ReviewOverlap','overlap',1000,999,0.85,100)
                    store.assert_called_once();retained.assert_called_once()
                    self.assertEqual(store.call_args.args[1][0]['source_file'],'accepted.txt')
                self.assertEqual(job['status'],'partial',job)
                self.assertEqual((job['files_completed'],job['files_failed'],job['chunks_stored']),(1,1,1))
                self.assertIn('over-budget.txt',job['errors'][0]);self.assertIn('per-file limit',job['errors'][0])
            finally:ingest_pipeline._jobs.pop(job_id,None)

    def test_live_helper_rejects_empty_parser_output_instead_of_vacuous_success(self):
        root=repository_root()
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        with patch.dict(os.environ,{'RAG_OVERLAP_REAL_EMBEDDING':'0'}), \
             patch.object(ingest_pipeline,'_parse_file',return_value=('',[])), \
             patch.object(ingest_pipeline.wc,'_collection_exists_sync',return_value=False), \
             patch.object(ingest_pipeline.wc,'get_client',return_value=MagicMock()), \
             patch.object(ingest_pipeline.wc,'_delete_collection_sync') as delete, \
             patch.object(ingest_pipeline.wc,'close_client'):
            with self.assertRaisesRegex(AssertionError,'parser returned no nonblank text'):
                runpy.run_path(str(root/'scripts/verify/overlap_chunks.py'))
            delete.assert_called_once()

    def test_entrypoint_uses_the_bounded_overlap_strategy(self):
        text = 'entrypoint' * 1000
        self.assertEqual(recover(chunker.chunk(text, 'overlap', 1000, 200), 200), text)

    def test_small_parameter_matrix_preserves_coverage_and_stated_bound(self):
        for size in range(1, 16):
            for overlap in range(size):
                for minimum in (0, 1, size, size+1, 2*size+1):
                    for length in (1, size, size+1, 2*size-1, 2*size+3):
                        text = ''.join(chr(65 + i % 26) for i in range(length))
                        with self.subTest(size=size, overlap=overlap, minimum=minimum, length=length):
                            self.check_windows(text,size,overlap,minimum)

    def test_ingest_worker_stores_bounded_overlapping_windows(self):
        text = '\n'.join('Inert source line ' + str(i) for i in range(400))
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'inert.txt'; source.write_text(text)
            job_id = 'overlap-controlled'
            job = {'status':'queued', 'files_completed':0, 'files_failed':0, 'chunks_stored':0, 'errors':[], 'files_total':1}
            with patch.dict(ingest_pipeline._jobs, {job_id:job}, clear=True), \
                 patch.object(ingest_pipeline, '_parse_file', return_value=(text,None)), \
                 patch.object(ingest_pipeline.wc, '_insert_chunks_sync') as store, \
                 patch.object(ingest_pipeline.wc, '_source_has_chunks_sync', return_value=False), \
                 patch.object(ingest_pipeline.sources, 'store'):
                ingest_pipeline._process_job_sync(job_id,[source],Path(directory),'ReviewOverlap','overlap',1000,200,0.85,100)
            self.assertEqual(job['status'], 'completed', job)
            saved = store.call_args.args[1]
            self.assertTrue(all(len(c['content']) <= 1000 for c in saved))
            self.assertEqual(recover([c['content'] for c in saved],200),text)
            self.assertEqual([c['chunk_index'] for c in saved],list(range(len(saved))))


class ImplementationTests(unittest.TestCase):
    def test_embedded_changed_sources_match_runtime(self):
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        text = (root / 'IMPLEMENTATION.md').read_text()
        for name,fence,language in [('api/services/chunker.py','```','python'),
                                     ('scripts/verify/overlap_chunks.py','```','python'),
                                     ('scripts/verify/README.md','````','markdown'),
                                     ('scripts/verify/02_ingest.sh','```','bash'),
                                     ('scripts/verify/08_overlap.sh','```','bash')]:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = text.index(header) + len(header)
                end = text.index('\n' + fence + '\n',start)
                self.assertEqual(text[start:end], (root/name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
