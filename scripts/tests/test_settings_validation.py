"""Direct/saved settings reject invalid inputs before model/backend work."""
import ast
import asyncio
import inspect
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, MagicMock, patch
api_dir = os.environ.get('RAG_TEST_API_DIR')
sys.path.insert(0, api_dir or str(Path(__file__).resolve().parents[2] / 'api'))
from fastapi.testclient import TestClient
from pydantic import ValidationError
from pydantic.fields import FieldInfo
from config import settings
from main import app
from models.schemas import CreateCollectionRequest, HnswConfig, IngestConfig, QueryRequest, RechunkRequest, ReembedRequest, SaveRetrievalConfigBody
from routers import collections, ingest, tuning as tuning_router, query, retrieval_config as retrieval_router
from services import weaviate_client as wc, chunker, rag_pipeline, ingest_config, retrieval_config, ingest_pipeline, packager

OVERLAP_RULE = 'chunk_overlap must be smaller than chunk_size for overlap/language'
NEEDS_REPOSITORY = 'needs the whole repository mounted (see scripts/verify/README.md)'


def repository_root():
    parents = Path(__file__).resolve().parents
    root = parents[2] if len(parents) > 2 else None
    return root if root is not None and (root / 'IMPLEMENTATION.md').is_file() else None


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.temp = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.stack.enter_context(patch.object(settings, 'upload_dir', self.temp))
        self.stack.enter_context(patch.object(ingest_config, '_DIR', None))
        self.stack.enter_context(patch.object(retrieval_config, '_DIR', None))
        self.exists = self.stack.enter_context(patch.object(wc, 'collection_exists', new=AsyncMock(return_value=True)))
        self.create = self.stack.enter_context(patch.object(wc, 'create_collection', new=AsyncMock()))
        self.ingest_job = self.stack.enter_context(patch.object(ingest.ingest_pipeline, 'start_ingest_job', new=AsyncMock(return_value='ingest')))
        self.tune_job = self.stack.enter_context(patch.object(tuning_router.tuning, 'start_tune_job', new=AsyncMock(return_value='tune')))
        self.query_work = self.stack.enter_context(patch.object(query.rag_pipeline, 'run_query', new=AsyncMock()))
        self.client = TestClient(app)  # No lifespan: these tests never connect a stack.
        self.addCleanup(self.client.close)

    def assert_rejected_without_work(self, path, payload):
        self.exists.reset_mock(); self.create.reset_mock(); self.ingest_job.reset_mock(); self.tune_job.reset_mock(); self.query_work.reset_mock()
        response = self.client.post(path, json=payload)
        self.assertEqual(response.status_code, 422, response.text)
        for work in (self.exists, self.create, self.ingest_job, self.tune_job, self.query_work): work.assert_not_called()
        self.assertEqual(list(Path(self.temp).rglob('*.json')), [])

    def test_invalid_collection_enums_and_hnsw_ranges_precede_backend(self):
        for update in ({'index_type': 'unknown'}, {'distance_metric': 'unknown'},
                       {'hnsw_config': {'efConstruction': 63}}, {'hnsw_config': {'efConstruction': 513}},
                       {'hnsw_config': {'maxConnections': 15}}, {'hnsw_config': {'maxConnections': 129}},
                       {'hnsw_config': {'ef': 15}}, {'hnsw_config': {'ef': 513}}):
            with self.subTest(update=update): self.assert_rejected_without_work('/collections', {'name': 'ReviewSettings', **update})

    def test_invalid_query_and_saved_retrieval_share_contract(self):
        for update in ({'retrieval_mode': 'unknown'}, {'response_format': 'unknown'}, {'top_k': 0},
                       {'top_k': 51}, {'alpha': -0.1}, {'alpha': 1.1}, {'top_k': True}, {'alpha': False}):
            for route in ('/query', '/retrieval/config'):
                with self.subTest(route=route, update=update):
                    self.assert_rejected_without_work(route, {'collection': 'ReviewSettings', 'question': 'synthetic', **update})

    def test_invalid_reindex_enums_precede_backend_and_job(self):
        for update in ({'index_type': 'unknown'}, {'distance_metric': 'unknown'}):
            self.assert_rejected_without_work('/tune/reindex', {'collection': 'ReviewSettings', **update})

    def test_saved_retrieval_ef_numeric_bounds(self):
        for ef in (0, 15, 513, True):
            self.assert_rejected_without_work('/retrieval/config', {'collection': 'ReviewSettings', 'ef': ef})

    def test_chunking_errors_on_saved_and_tuning_surfaces_precede_work(self):
        for update in ({'chunking_strategy': 'unknown'}, {'chunk_size': 0}, {'chunk_size': -1},
                       {'chunk_size': 49}, {'chunk_size': 6001}, {'min_chunk_size': 6001},
                       {'chunk_overlap': -1}, {'chunk_overlap': 1000}, {'min_chunk_size': -1},
                       {'similarity_threshold': -0.1}, {'similarity_threshold': 1.1},
                       {'chunk_size': True}, {'similarity_threshold': False}):
            for route in ('/ingest/config', '/tune/rechunk', '/tune/reembed'):
                with self.subTest(route=route, update=update): self.assert_rejected_without_work(route, {'collection': 'ReviewSettings', **update})

    def test_multipart_invalid_chunking_precedes_collection_lookup_and_file_staging(self):
        for update in ({'strategy': 'unknown'}, {'chunk_size': '0'}, {'chunk_size': '49'},
                       {'chunk_size': '6001'}, {'min_chunk_size': '6001'}, {'chunk_overlap': '-1'},
                       {'chunk_overlap': '1000'}, {'similarity_threshold': 'nan'}, {'min_chunk_size': '-1'}):
            self.exists.reset_mock(); self.ingest_job.reset_mock()
            response = self.client.post('/ingest/upload', data={'collection': 'ReviewSettings', **update}, files={'files': ('source.txt', b'inert synthetic text')})
            self.assertEqual(response.status_code, 422, response.text)
            self.exists.assert_not_called(); self.ingest_job.assert_not_called()
            self.assertEqual(list(Path(self.temp).iterdir()), [])

    def test_multipart_non_numeric_settings_are_invalid_settings(self):
        # #108: these used to fail form parsing first, as INVALID_PARAMETER.
        for update in ({'chunk_size': 'abc'}, {'chunk_size': '1.5'}, {'chunk_overlap': 'abc'},
                       {'min_chunk_size': 'abc'}, {'similarity_threshold': 'abc'}):
            with self.subTest(update=update):
                self.exists.reset_mock(); self.ingest_job.reset_mock()
                response = self.client.post('/ingest/upload', data={'collection': 'ReviewSettings', **update}, files={'files': ('source.txt', b'inert synthetic text')})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()['error']['code'], 'INVALID_SETTINGS')
                self.exists.assert_not_called(); self.ingest_job.assert_not_called()
                self.assertEqual(list(Path(self.temp).iterdir()), [])

    def test_valid_multipart_values_reach_job_as_numbers(self):
        data = {'collection': 'ReviewSettings', 'chunk_size': '800', 'chunk_overlap': '100', 'similarity_threshold': '0.5', 'min_chunk_size': '50'}
        response = self.client.post('/ingest/upload', data=data, files={'files': ('source.txt', b'inert synthetic text')})
        self.assertEqual(response.status_code, 202, response.text)
        kwargs = self.ingest_job.call_args.kwargs
        self.assertEqual([(kwargs[k], type(kwargs[k])) for k in ('chunk_size', 'chunk_overlap', 'similarity_threshold', 'min_chunk_size')],
                         [(800, int), (100, int), (0.5, float), (50, int)])

    def test_tuning_relationship_reply_is_the_fixed_message(self):
        for route in ('/tune/rechunk', '/tune/reembed'):
            with self.subTest(route=route):
                response = self.client.post(route, json={'collection': 'ReviewSettings', 'chunk_size': 100, 'chunk_overlap': 500})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()['error']['code'], 'INVALID_PARAMETER')
                self.assertEqual(response.json()['error']['detail'], [{'type': 'value_error', 'loc': ['body'], 'msg': 'Value error, ' + OVERLAP_RULE}])
                self.tune_job.assert_not_called()

    def test_nonfinite_json_is_a_serializable_422(self):
        for token in ('NaN', 'Infinity', '-Infinity'):
            for route, field in (('/query', 'alpha'), ('/retrieval/config', 'alpha'), ('/ingest/config', 'similarity_threshold'), ('/tune/rechunk', 'similarity_threshold')):
                self.exists.reset_mock(); self.query_work.reset_mock()
                body = '{"question":"synthetic","collection":"ReviewSettings","' + field + '":' + token + '}'
                response = self.client.post(route, content=body, headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertTrue(response.json()['error']['detail'])
                self.assertEqual(response.json()['error']['code'],'INVALID_PARAMETER')
                self.exists.assert_not_called(); self.query_work.assert_not_called()

    def test_valid_defaults_and_boundary_settings(self):
        self.assertEqual(CreateCollectionRequest(name='ReviewSettings').hnsw_config.model_dump(), {'efConstruction': 128, 'maxConnections': 64, 'ef': 64})
        for values in ((64, 16, 16), (512, 128, 512)):
            HnswConfig(efConstruction=values[0], maxConnections=values[1], ef=values[2])
        for mode in ('hnsw', 'flat', 'hybrid', 'semantic'):
            QueryRequest(question='synthetic', collection='ReviewSettings', retrieval_mode=mode, top_k=1, alpha=0)
            QueryRequest(question='synthetic', collection='ReviewSettings', retrieval_mode=mode, top_k=50, alpha=1)
        self.assertEqual(RechunkRequest(collection='ReviewSettings', min_chunk_size=0).chunking()['min_chunk_size'], 0)
        self.assertFalse(ReembedRequest(collection='ReviewSettings').has_chunking())
        self.assertFalse(ReembedRequest(collection='ReviewSettings', chunk_size=None).has_chunking())
        self.assertEqual(RechunkRequest(collection='ReviewSettings').chunking()['chunk_size'], 1000)

    def test_applicable_overlap_and_minimum_relationships_preserve_ignored_defaults(self):
        IngestConfig(chunking_strategy='fixed', chunk_size=150, min_chunk_size=40)  # default overlap200 is unused
        IngestConfig(chunking_strategy='context_aware', chunk_size=150, min_chunk_size=40, chunk_overlap=1000)
        IngestConfig(chunking_strategy='semantic', min_chunk_size=2000)
        for strategy in ('overlap', 'language'):
            with self.assertRaises(ValidationError): IngestConfig(chunking_strategy=strategy, chunk_overlap=1000)
        IngestConfig(chunking_strategy='overlap', chunk_size=201, chunk_overlap=200, min_chunk_size=0)

    def test_fixed_minimum_can_exceed_split_target_as_existing_acceptance_requires(self):
        body = {'collection':'ReviewSettings', 'chunking_strategy':'fixed', 'chunk_size':60, 'min_chunk_size':100}
        self.assertEqual(self.client.post('/ingest/config',json=body).status_code,201)
        self.assertEqual(self.client.post('/tune/rechunk',json=body).status_code,202)
        response = self.client.post('/ingest/upload',data={'collection':'ReviewSettings','strategy':'fixed','chunk_size':'60','min_chunk_size':'100'},files={'files':('tiny.txt',b'inert synthetic text')})
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.ingest_job.call_args.kwargs['min_chunk_size'],100)
        self.assertEqual(len(chunker.chunk('x'*169,'fixed',chunk_size=60,min_chunk_size=100)),1)

    def test_small_target_with_omitted_strategy_keeps_default_overlap_validation(self):
        for route in ('/tune/rechunk','/tune/reembed'):
            self.assert_rejected_without_work(route,{'collection':'ReviewSettings','chunk_size':80})
        response = self.client.post('/tune/reembed',json={'collection':'ReviewSettings','chunking_strategy':'fixed','chunk_size':80})
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(self.tune_job.call_args.args[2]['chunking']['strategy'],'fixed')

    def test_chunk_bounds_roundtrip_and_neighbors_are_rejected(self):
        # #53's Decision: chunk_size 50-6000, min_chunk_size 0-6000.
        for size, minimum in ((50, 0), (6000, 6000)):
            body={'collection':'ReviewSettings','chunking_strategy':'fixed','chunk_size':size,'min_chunk_size':minimum}
            self.assertEqual(self.client.post('/ingest/config',json=body).status_code,201)
            saved=self.client.get('/ingest/config/ReviewSettings').json()
            self.assertEqual((saved['chunk_size'],saved['min_chunk_size']),(size,minimum))
            for bad in ({'chunk_size':49} if size == 50 else {'chunk_size':6001}, {'min_chunk_size':6001}):
                response=self.client.post('/ingest/config',json={**body,**bad})
                self.assertEqual(response.status_code,422)
                self.assertEqual(response.json()['error']['code'],'INVALID_PARAMETER')
                self.assertEqual(self.client.get('/ingest/config/ReviewSettings').json(),saved)

    def test_saved_config_from_before_the_bounds_is_served_until_saved_again(self):
        legacy={'collection':'ReviewSettings','chunking_strategy':'overlap','chunk_size':16000,
                'chunk_overlap':200,'similarity_threshold':None,'min_chunk_size':8000}
        ingest_config.save(legacy)
        response=self.client.get('/ingest/config/ReviewSettings')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual((response.json()['chunk_size'],response.json()['min_chunk_size']),(16000,8000))
        self.assertEqual(self.client.post('/ingest/config',json=legacy).status_code,422)
        self.assertEqual(ingest_config.load('ReviewSettings')['chunk_size'],16000)
        for route in ('/tune/rechunk','/tune/reembed'):
            self.assertEqual(self.client.post(route,json=legacy).status_code,422)
        self.tune_job.assert_not_called()

    def test_saved_config_from_before_the_bounds_exports_unclamped(self):
        # Decision (#53): "export still packages them, never clamped" -- the
        # packager reads the saved config the same way the GET route does,
        # with no revalidation against the new chunk_size/min_chunk_size bounds.
        legacy={'collection':'ReviewSettings','chunking_strategy':'overlap','chunk_size':16000,
                'chunk_overlap':200,'similarity_threshold':None,'min_chunk_size':8000}
        ingest_config.save(legacy)
        self.assertEqual(packager._ingest_config('ReviewSettings'), legacy)

    def test_saved_valid_ingest_and_retrieval_round_trips(self):
        ingest_body = {'collection': 'ReviewSettings', 'chunking_strategy': 'fixed', 'chunk_size': 150, 'min_chunk_size': 40}
        response = self.client.post('/ingest/config', json=ingest_body)
        self.assertEqual(response.status_code, 201, response.text)
        saved = self.client.get('/ingest/config/ReviewSettings').json()
        self.assertEqual(saved['chunk_size'], 150); self.assertEqual(saved['chunk_overlap'], 200); self.assertFalse(saved['is_default'])
        response = self.client.post('/retrieval/config', json={'collection': 'ReviewSettings', 'retrieval_mode': 'hybrid', 'top_k': 50, 'alpha': 1, 'ef': 512, 'response_format': 'engineer'})
        self.assertEqual(response.status_code, 201, response.text)
        saved = self.client.get('/retrieval/config/ReviewSettings').json()
        self.assertEqual(saved['top_k'], 50); self.assertEqual(saved['ef'], 512); self.assertFalse(saved['is_default'])

    def test_valid_multipart_defaults_reach_existing_job_route(self):
        response = self.client.post('/ingest/upload', data={'collection': 'ReviewSettings'}, files={'files': ('source.txt', b'inert synthetic text')})
        self.assertEqual(response.status_code, 202, response.text)
        self.ingest_job.assert_awaited_once()
        self.assertEqual(self.ingest_job.call_args.kwargs['chunk_overlap'], 200)

    def test_unconfigured_reembed_preserves_no_rechunk_behavior(self):
        response = self.client.post('/tune/reembed', json={'collection': 'ReviewSettings'})
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(self.tune_job.call_args.args[2], {'chunking': None})


class InternalBoundaryTests(unittest.TestCase):
    def test_collection_creation_rejects_bad_settings_before_client_access(self):
        for index, distance, config in (('unknown','cosine',{}), ('hnsw','unknown',{}), ('hnsw','cosine',{'ef':0})):
            with patch.object(wc, 'get_client') as client:
                with self.assertRaises(ValidationError): wc._create_collection_sync('ReviewSettings', index, distance, config)
                client.assert_not_called()

    def test_tuning_relationship_error_is_a_plain_value_error(self):
        # #108: a nested ValidationError carried the inner model's input and text.
        for model in (RechunkRequest, ReembedRequest):
            with self.subTest(model=model.__name__):
                with self.assertRaises(ValidationError) as caught: model(collection='ReviewSettings', chunk_size=100, chunk_overlap=500)
                errors = caught.exception.errors()
                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]['input']['collection'], 'ReviewSettings')
                self.assertIs(type(errors[0]['ctx']['error']), ValueError)
                self.assertEqual(str(errors[0]['ctx']['error']), OVERLAP_RULE)

    def test_stored_hnsw_settings_are_preserved_while_new_requests_remain_strict(self):
        legacy={'efConstruction':1000,'maxConnections':256,'ef':-1}
        with self.assertRaises(ValidationError):
            CreateCollectionRequest(name='ReviewStored',hnsw_config=legacy)
        client=MagicMock()
        with patch.object(wc,'get_client',return_value=client), \
             patch.object(wc.Configure.VectorIndex,'hnsw') as configure:
            wc._create_collection_sync('ReviewStored','hnsw','cosine',legacy,preserve_hnsw=True)
            self.assertEqual(configure.call_args.kwargs['ef_construction'],1000)
            self.assertEqual(configure.call_args.kwargs['max_connections'],256)
            self.assertEqual(configure.call_args.kwargs['ef'],-1)
            client.collections.create.assert_called_once()
        for index,distance,config in (('invalid','cosine',legacy),('hnsw','invalid',legacy),
                ('hnsw','cosine',{'ef':0}),('hnsw','cosine',{'ef':False}),
                ('hnsw','cosine',{'efConstruction':0}),('hnsw','cosine',{'maxConnections':-1})):
            with patch.object(wc,'get_client') as backend:
                with self.assertRaises(ValidationError):
                    wc._create_collection_sync('ReviewStored',index,distance,config,preserve_hnsw=True)
                backend.assert_not_called()

    def test_package_creation_explicitly_preserves_stored_hnsw(self):
        from services import importer
        legacy={'efConstruction':1000,'maxConnections':256,'ef':-1}
        with tempfile.TemporaryDirectory() as directory, patch.object(wc,'_create_collection_sync') as create:
            pkg=Path(directory);(pkg/'collection.json').write_text(json.dumps({'hnsw_config':legacy}))
            importer._create_from_package('ReviewStored',pkg)
            create.assert_called_once_with('ReviewStored','hnsw','cosine',legacy,preserve_hnsw=True,description=None)

    def test_rebuild_preserves_stored_hnsw_for_staging_and_replacement(self):
        from services import tuning
        legacy={'efConstruction':1000,'maxConnections':256,'ef':-1}
        config={'index_type':'hnsw','distance_metric':'cosine','hnsw_config':legacy}
        client=MagicMock();client.collections.get.return_value.iterator.return_value=[]
        client.collections.get.return_value.aggregate.over_all.return_value.total_count=0
        client.collections.get.return_value.batch.dynamic.return_value.__enter__.return_value.number_errors=0
        # _rebuild writes its cutover record under UPLOAD_DIR: keep it in a temporary one.
        with tempfile.TemporaryDirectory() as uploads, \
             patch.object(settings,'upload_dir',uploads), \
             patch.object(wc,'_collection_config_sync',return_value=config), \
             patch.object(wc,'get_client',return_value=client), \
             patch.object(wc,'_create_collection_sync') as create, \
             patch.object(wc,'_insert_chunks_sync'), \
             patch.object(tuning.collection_recovery,'begin',return_value={'staging':'ReviewStored__test','state':'scratch','operation_id':'test'}), \
             patch.object(tuning.collection_recovery,'retain'), \
             patch.object(tuning.collection_recovery,'discard'), \
             patch.object(tuning.batch_write,'insert',return_value=0):
            self.assertEqual(tuning._rebuild('ReviewStored',[],None,None,None),0)
            self.assertEqual(create.call_count,2)
            for call in create.call_args_list:
                self.assertEqual(call.args[1:],('hnsw','cosine',legacy))
                expected = {'preserve_hnsw': True}
                if call.args[0] == 'ReviewStored':
                    expected['description'] = 'rag-tune:test'
                self.assertEqual(call.kwargs, expected)

    def test_chunker_rejects_before_semantic_model_or_splitter_work(self):
        for strategy, size, overlap in (('unknown',1000,200), ('semantic',0,200), ('overlap',1000,1000)):
            with patch.object(chunker, '_get_semantic_model') as model:
                with self.assertRaises(ValidationError): chunker.chunk('inert text',strategy,chunk_size=size,chunk_overlap_size=overlap)
                model.assert_not_called()

    def test_context_fallback_ignores_overlap_as_documented(self):
        with patch.object(chunker, 'chunk_language', return_value=['inert']) as split:
            chunker.chunk('inert', 'context_aware', chunk_size=150, chunk_overlap_size=1000, min_chunk_size=40)
        self.assertEqual(split.call_args.args[2], 0)

    def test_direct_ingest_rejects_before_upload_staging_or_job_creation(self):
        with patch.object(ingest_pipeline.tempfile, 'mkdtemp') as stage, patch.dict(ingest_pipeline._jobs, {}, clear=True):
            with self.assertRaises(ValidationError):
                asyncio.run(ingest_pipeline.start_ingest_job([], 'ReviewSettings', 'unknown', 1000, 200, 0.85, 100))
            stage.assert_not_called()
            self.assertEqual(ingest_pipeline._jobs, {})

    def test_query_rejects_before_llm_or_retrieval(self):
        # The HTTP test patch is absent here: exercise the real service entry.
        with patch.object(rag_pipeline.ollama, 'chat', new=AsyncMock()) as chat:
            with self.assertRaises(ValidationError): asyncio.run(rag_pipeline.run_query('synthetic','ReviewSettings','unknown',5,0.5,False,'end_user'))
            chat.assert_not_called()


class ApiPathFallbackTests(unittest.TestCase):
    # #135: the repository fallback was computed even with RAG_TEST_API_DIR set.
    def api_path_setup(self, source):
        tree = ast.parse(source)
        start = next(i for i, node in enumerate(tree.body) if 'RAG_TEST_API_DIR' in ast.get_source_segment(source, node))
        for end in range(start, len(tree.body)):
            node = tree.body[end]
            if isinstance(node, ast.Expr) and ast.get_source_segment(source, node.value.func) == 'sys.path.insert':
                return ast.Module(body=tree.body[start:end + 1], type_ignores=[])
        self.fail('no sys.path.insert after RAG_TEST_API_DIR')

    def test_fallback_is_only_computed_when_the_variable_is_unset(self):
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        def evaluated(*args): raise AssertionError('fallback evaluated')
        cases = (('set', {'RAG_TEST_API_DIR': '/synthetic/api'}, evaluated, ['/synthetic/api']),
                 ('empty', {'RAG_TEST_API_DIR': ''}, Path, ['/synthetic/repo/api']),
                 ('unset', {}, Path, ['/synthetic/repo/api']))
        for name in ('scripts/verify/reindex_verifier_cases.py', 'scripts/tests/test_retrieval_controls.py'):
            setup = compile(self.api_path_setup((root / name).read_text()), name, 'exec')
            for case, environ, path, expected in cases:
                with self.subTest(file=name, case=case):
                    namespace = {'os': SimpleNamespace(environ=environ), 'sys': SimpleNamespace(path=[]),
                                 'Path': path, '__file__': '/synthetic/repo/scripts/x/file.py'}
                    exec(setup, namespace)
                    self.assertEqual(namespace['sys'].path, expected)

    # #155: the remaining defaults were computed even with their variable set.
    def test_listed_defaults_are_lazy(self):
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        def evaluated(*args): raise AssertionError('fallback evaluated')
        api, repo = '/synthetic/repo/api', '/synthetic/repo/scripts/x/'
        rows = (('scripts/tests/test_overlap_chunks.py', 'RAG_TEST_API_DIR', 1, False, api),
                ('scripts/tests/test_insert_chunks.py', 'RAG_TEST_API_DIR', 1, False, api),
                ('scripts/tests/test_model_bundle.py', 'RAG_TEST_API_DIR', 2, False, api),
                ('scripts/tests/test_session_validity.py', 'RAG_TEST_API_DIR', 1, False, api),
                ('scripts/tests/test_chunk_sampling.py', 'RAG_TEST_API_DIR', 1, False, api),
                ('scripts/tests/test_batch_recovery.py', 'RAG_TEST_API_DIR', 1, False, api),
                ('scripts/tests/test_collection_writes.py', 'RAG_TEST_API_DIR', 1, True, api),
                ('scripts/verify/reindex_cases.py', 'RAG_TEST_API_DIR', 1, True, api),
                ('scripts/verify/session_identity_cases.py', 'RAG_TEST_API_DIR', 1, True, api),
                ('scripts/verify/session_persistence_cases.py', 'RAG_TEST_API_DIR', 3, True, api),
                ('scripts/verify/reindex_verifier_cases.py', 'RAG_REINDEX_VERIFIER_SOURCE', 1, False, repo + 'reindex.py'),
                ('scripts/verify/reindex_verifier_cases.py', 'RAG_VERIFIER_LIB', 1, False, repo + 'lib.sh'))
        for name, variable, occurrences, guarded, unset in rows:
            source = (root / name).read_text()
            tree = ast.parse(source)
            def reads(node):
                return (isinstance(node, ast.Call) and ast.get_source_segment(source, node.func) == 'os.environ.get'
                        and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == variable)
            calls = [node for node in ast.walk(tree) if reads(node)]
            lazy = [node for node in ast.walk(tree) if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
                    and reads(node.values[0]) and len(node.values[0].args) == 1]
            with self.subTest(file=name, variable=variable):
                self.assertEqual(len(calls), occurrences)
                self.assertEqual(len(lazy), occurrences, 'os.environ.get(name, fallback) computes the fallback')
            # Only the repository's own fallback expression runs, against fake os/Path.
            for expression in lazy:
                code = compile(ast.fix_missing_locations(ast.Expression(body=expression)), name, 'eval')
                cases = [('set', {variable: '/synthetic/value'}, evaluated, '/synthetic/value'),
                         ('empty', {variable: ''}, Path, unset), ('unset', {}, Path, unset)]
                if guarded: cases.append(('stdin', {}, Path, '/app'))
                for case, environ, path, expected in cases:
                    with self.subTest(file=name, variable=variable, line=expression.lineno, case=case):
                        script = '<stdin>' if case == 'stdin' else repo + 'file.py'
                        reference = SimpleNamespace(with_name=evaluated) if path is evaluated else Path(repo + 'reindex.py')
                        namespace = {'os': SimpleNamespace(environ=environ), 'Path': path, '__file__': script, 'source': reference}
                        self.assertEqual(eval(code, namespace), expected)

    def test_no_environment_default_is_computed(self):
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        for path in sorted([*(root / 'scripts/tests').glob('*.py'), *(root / 'scripts/verify').glob('*.py')]):
            source = path.read_text()
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Call) and ast.get_source_segment(source, node.func) == 'os.environ.get' and len(node.args) > 1:
                    with self.subTest(file=str(path.relative_to(root)), line=node.lineno):
                        self.assertIsInstance(node.args[1], ast.Constant, ast.get_source_segment(source, node))


class SpecificationTests(unittest.TestCase):
    def test_upload_form_fields_match_the_route(self):
        # #135: the spec named the strategy field chunking_strategy and required it.
        root = repository_root()
        if root is None: self.skipTest(NEEDS_REPOSITORY)
        lines = (root / 'SPECIFICATIONS.md').read_text().splitlines()
        start = lines.index('**Form fields:**', lines.index('POST /ingest/upload'))
        documented = {}
        for line in lines[start + 1:]:
            if documented and not line.startswith('|'): break
            if line.startswith('| `'):
                cells = [cell.strip() for cell in line.strip('|').split('|')]
                documented[cells[0].strip('`')] = {'Yes': True, 'No': False}[cells[2]]
        route = {name: parameter.default.is_required()
                 for name, parameter in inspect.signature(ingest.ingest_upload).parameters.items()
                 if isinstance(parameter.default, FieldInfo)}
        self.assertEqual(documented, route)


class ImplementationTests(unittest.TestCase):
    def test_ingest_config_has_no_obsolete_strategy_constant(self):
        self.assertFalse(hasattr(ingest_config, "CHUNKING_STRATEGIES"))

    def test_embedded_changed_sources_match_runtime(self):
        parents = Path(__file__).resolve().parents
        root = parents[2] if len(parents) > 2 else None
        if root is None or not (root / 'IMPLEMENTATION.md').is_file():
            self.skipTest('needs the whole repository mounted (see scripts/verify/README.md)')
        text = (root / 'IMPLEMENTATION.md').read_text()
        names = ['api/models/schemas.py', 'api/main.py', 'api/routers/ingest.py',
                 'api/services/chunker.py', 'api/services/ingest_pipeline.py',
                 'api/services/ingest_config.py',
                 'api/services/rag_pipeline.py', 'api/services/weaviate_client.py',
                 'scripts/verify/settings_validation.py', 'scripts/verify/05_transfer.sh',
                 'api/services/tuning.py','api/services/importer.py',
                 'scripts/verify/all.sh','scripts/verify/07_settings.sh']
        for name in names:
            with self.subTest(file=name):
                language = 'bash' if name.endswith('.sh') else 'python'
                header = '### ' + name + '\n\n```' + language + '\n'
                start = text.index(header) + len(header)
                end = text.index('\n```\n', start)
                self.assertEqual(text[start:end], (root / name).read_text().rstrip('\n'))


if __name__ == '__main__': unittest.main()
