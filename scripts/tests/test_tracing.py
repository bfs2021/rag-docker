"""Synthetic #283 producer/worker tests; no backend or hosted collector."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import contextlib
import gzip
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, AsyncMock

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from services import telemetry as ot, exporter, importer, tuning, goldstandard as gs
from services import ingest_pipeline as ingest, weaviate_client as wc, ollama_client as ollama
from config import settings
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
import httpx
from fastapi import FastAPI
from routers import query, ingest as ingest_router, transfer, tuning as tuning_router, goldstandard

SECRET = 'SENTINEL-secret-document-path-prompt'


class TracingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.stack = contextlib.ExitStack()
        for field in ('upload_dir', 'sources_dir', 'exports_dir'):
            self.stack.enter_context(patch.object(settings, field, self.temp.name))
        self.memory = InMemorySpanExporter()
        self.provider = TracerProvider(sampler=TraceIdRatioBased(1), shutdown_on_exit=False)
        self.provider.add_span_processor(SimpleSpanProcessor(self.memory))
        self.runtime = ot.Runtime(ot.Config())
        self.runtime.tracer = self.provider.get_tracer('test')
        self.app = FastAPI()
        self.app.state.telemetry = self.runtime
        self.app.add_middleware(ot.RequestTracing)
        for router in (query.router, ingest_router.router, transfer.router, tuning_router.router, goldstandard.router):
            self.app.include_router(router)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        self.provider.shutdown()
        self.stack.close()
        self.temp.cleanup()

    def spans(self, name=None):
        result = self.memory.get_finished_spans()
        return [s for s in result if name is None or s.name == name or (name == 'rag.request' and s.kind == trace.SpanKind.SERVER)]

    def wire(self):
        payload = encode_spans(self.spans()).SerializeToString()
        clean = ot.sanitize_wire(payload, 'traces', ot.Config(batch_size=512))
        self.assertNotIn(SECRET.encode(), clean)
        return ExportTraceServiceRequest.FromString(clean).resource_spans[0].scope_spans[0].spans

    async def until(self, predicate):
        for _ in range(1000):
            if predicate():
                return
            await asyncio.sleep(.002)
        self.fail('worker did not complete')

    def lineage(self, job_name):
        jobs = self.spans(job_name)
        requests = {s.context.span_id: s for s in self.spans('rag.request')}
        self.assertTrue(jobs)
        for job in jobs:
            self.assertIn(job.parent.span_id, requests)
            self.assertEqual(job.context.trace_id, requests[job.parent.span_id].context.trace_id)
        self.wire()

    async def test_query_real_pipeline_and_dependency_error(self):
        collection = NS(query=NS(near_vector=lambda **kw: NS(objects=[])))
        fake_client = NS(collections=NS(get=lambda name: collection))
        async def chat(*args):
            return SECRET
        async def embed(*args):
            return [1.0]
        with patch.object(wc, 'collection_exists', AsyncMock(return_value=True)), patch.object(wc, 'get_client', return_value=fake_client), patch.object(ollama, 'chat', ot.traced('ollama.chat')(chat)), patch.object(ollama, 'embed', ot.traced('ollama.embed')(embed)):
            response = await self.client.post('/query', json={'question': SECRET, 'collection': 'Corpus', 'retrieval_mode': 'hnsw'})
        self.assertEqual(response.status_code, 200, response.text)
        for name in ('rag.query', 'rag.reformulate', 'rag.retrieval', 'rag.synthesis', 'weaviate.query'):
            self.assertTrue(self.spans(name), name)
        dep = self.spans('weaviate.query')[0]
        self.assertGreater(dep.end_time, dep.start_time)
        self.assertEqual(next(s.name for s in self.spans() if s.context.span_id == dep.parent.span_id), 'rag.retrieval')
        self.wire()
        with ot.bind((self.runtime, None)), self.assertRaises(TimeoutError):
            ot.call('weaviate.query', lambda: (_ for _ in ()).throw(TimeoutError(SECRET)))
        self.assertEqual(self.spans('weaviate.query')[-1].attributes['error.type'], 'timeout')

    async def test_headers_routes_and_external_sampling(self):
        tid, sid = 'a' * 32, 'b' * 16
        good = f'00-{tid}-{sid}-00'
        response = await self.client.get('/export/job/' + SECRET, headers={'traceparent': good, 'baggage': SECRET, 'tracestate': SECRET})
        self.assertEqual(response.status_code, 404)
        server = self.spans('rag.request')[-1]
        self.assertIsNone(server.parent)
        self.assertNotEqual(server.context.trace_id, int(tid, 16))
        self.assertEqual(server.links[0].context.trace_id, int(tid, 16))
        self.assertEqual(server.attributes['http.route'], 'GET /export/job/{job_id}')
        self.assertEqual(server.attributes['http.status_class'], '4xx')
        for headers in ({'traceparent': 'x' * 10000}, {'traceparent': good.upper()}, {'traceparent': f'00-{tid}-'+ '0'*16+'-01'}, [('traceparent', good), ('traceparent', good)], {'traceparent': good.replace('00-', 'ff-', 1)}):
            await self.client.get('/'+SECRET, headers=headers)
            self.assertFalse(self.spans('rag.request')[-1].links)
            self.assertEqual(self.spans('rag.request')[-1].attributes['http.route'], 'unmatched')
        self.wire()
        self.runtime.tracer = TracerProvider(sampler=TraceIdRatioBased(0)).get_tracer('zero')
        count = len(self.spans())
        await self.client.get('/'+SECRET, headers={'traceparent': good[:-2]+'01'})
        self.assertEqual(len(self.spans()), count)

    async def test_outcome_admission_and_classification(self):
        with ot.bind((self.runtime, None)):
            for explicit in (False, True):
                with ot.span('rag.query') as current:
                    for invalid in (SECRET, None, 1, True, [], {}, ('error',)):
                        ot.outcome(invalid, ValueError(SECRET), current if explicit else None)
                        self.assertEqual(dict(current.attributes), {'rag.outcome': 'ok'})
                        self.assertEqual(current.status.status_code, trace.StatusCode.UNSET)
            for value in ('ok', 'error', 'partial', 'cancelled'):
                with ot.span('rag.query') as current:
                    ot.outcome(value, ValueError(SECRET))
                result = self.spans('rag.query')[-1]
                self.assertEqual(result.attributes['rag.outcome'], value)
                self.assertEqual(result.attributes.get('error.type'),
                                 'validation' if value != 'cancelled' else None)
            for exc, category in ((TimeoutError(SECRET), 'timeout'),
                                  (ConnectionError(SECRET), 'connection'),
                                  (ValueError(SECRET), 'validation'),
                                  (RuntimeError(SECRET), 'internal')):
                with self.assertRaises(type(exc)) as caught:
                    with ot.span('rag.query'):
                        raise exc
                self.assertIs(caught.exception, exc)
                self.assertEqual(self.spans('rag.query')[-1].attributes['error.type'], category)
        self.wire()

    async def test_span_cancellation_has_no_internal_error(self):
        with ot.bind((self.runtime, None)):
            for exc in (asyncio.CancelledError(SECRET), GeneratorExit(SECRET)):
                with ot.span('rag.query') as parent:
                    with self.assertRaises(type(exc)) as caught:
                        with ot.span('rag.retrieval'):
                            raise exc
                    self.assertIs(caught.exception, exc)
                    self.assertIs(trace.get_current_span(), parent)
                result = self.spans('rag.retrieval')[-1]
                self.assertEqual(result.attributes['rag.outcome'], 'cancelled')
                self.assertNotIn('error.type', result.attributes)
        self.wire()

    async def test_iterator_terminal_outcomes_and_context(self):
        for phase in ('factory', 'iter', 'next', 'yield'):
            for exc in (asyncio.CancelledError(SECRET), GeneratorExit(SECRET), ValueError(SECRET)):
                with self.subTest(phase=phase, exc=type(exc).__name__):
                    def fail():
                        raise exc
                    class Source:
                        def __iter__(self):
                            if phase == 'iter':
                                fail()
                            return self
                        def __next__(self):
                            if phase == 'next':
                                fail()
                            return 1
                    factory = fail if phase == 'factory' else Source
                    before = len(self.spans('weaviate.iterate'))
                    with ot.bind((self.runtime, None)), ot.span('rag.export') as parent:
                        iterator = ot.iterate(factory)
                        with self.assertRaises(type(exc)) as caught:
                            if phase == 'yield':
                                self.assertEqual(next(iterator), 1)
                                self.assertIs(trace.get_current_span(), parent)
                                iterator.throw(exc)
                            else:
                                next(iterator)
                        self.assertIs(caught.exception, exc)
                        self.assertIs(trace.get_current_span(), parent)
                        iterator.close()
                    self.assertEqual(len(self.spans('weaviate.iterate')), before + 1)
                    result = self.spans('weaviate.iterate')[-1]
                    cancelled = isinstance(exc, (asyncio.CancelledError, GeneratorExit))
                    self.assertEqual(result.attributes['rag.outcome'], 'cancelled' if cancelled else 'error')
                    self.assertEqual(result.attributes.get('error.type'), None if cancelled else 'validation')
                    self.assertEqual(result.parent.span_id, parent.get_span_context().span_id)
        with ot.bind((self.runtime, None)), ot.span('rag.export') as parent:
            iterator = ot.iterate(lambda: iter([1, 2]))
            self.assertEqual(next(iterator), 1)
            iterator.close()
            self.assertEqual(self.spans('weaviate.iterate')[-1].attributes['rag.outcome'], 'cancelled')
            self.assertNotIn('error.type', self.spans('weaviate.iterate')[-1].attributes)
            self.assertIs(trace.get_current_span(), parent)
            self.assertEqual(list(ot.iterate(lambda: iter([1, 2]))), [1, 2])
            self.assertEqual(self.spans('weaviate.iterate')[-1].attributes['rag.outcome'], 'ok')
            self.assertIs(trace.get_current_span(), parent)
        self.wire()

    async def test_health_covers_connection_and_readiness(self):
        exc = ConnectionError(SECRET)
        with patch.object(wc, '_client', None), patch.object(wc.weaviate, 'connect_to_custom', side_effect=exc), ot.bind((self.runtime, None)):
            with self.assertRaises(ConnectionError) as caught:
                await wc.check_health()
        self.assertIs(caught.exception, exc)
        health = self.spans('weaviate.health')[-1]
        connect = self.spans('weaviate.connect')[-1]
        self.assertEqual(connect.parent.span_id, health.context.span_id)
        for result in (health, connect):
            self.assertEqual(result.attributes['rag.outcome'], 'error')
            self.assertEqual(result.attributes['error.type'], 'connection')
        for ready in (True, False, TimeoutError(SECRET)):
            def check():
                if isinstance(ready, BaseException):
                    raise ready
                return ready
            with patch.object(wc, 'get_client', return_value=NS(is_ready=check)), ot.bind((self.runtime, None)):
                if isinstance(ready, BaseException):
                    with self.assertRaises(TimeoutError) as caught:
                        await wc.check_health()
                    self.assertEqual(caught.exception.args, ready.args)
                else:
                    self.assertIs(await wc.check_health(), ready)
            result = self.spans('weaviate.health')[-1]
            self.assertEqual(result.attributes['rag.outcome'], 'ok' if ready is True else 'error')
            self.assertEqual(result.attributes.get('error.type'), 'timeout' if isinstance(ready, BaseException) else None)
        self.wire()

    async def test_all_trace_flag_bytes_preserve_only_sampled_bit(self):
        tid, sid = 'a' * 32, 'b' * 16
        for flags in range(256):
            value = f'00-{tid}-{sid}-{flags:02x}'
            links = ot.remote_link([(b'traceparent', value.encode())])
            self.assertEqual(len(links), 1)
            self.assertEqual(int(links[0].context.trace_flags), flags & 1)
        for flags in ('02', '03', 'fe', 'ff'):
            await self.client.get('/' + SECRET, headers={'traceparent': f'00-{tid}-{sid}-{flags}'})
            server = self.spans('rag.request')[-1]
            self.assertIsNone(server.parent)
            self.assertNotEqual(server.context.trace_id, int(tid, 16))
            self.assertEqual(server.links[0].context.trace_id, int(tid, 16))
            self.assertEqual(int(server.links[0].context.trace_flags), int(flags, 16) & 1)
        for flags in ('0G', 'FF', 'g0', '0', '000', ' 1'):
            self.assertFalse(ot.remote_link([(b'traceparent', f'00-{tid}-{sid}-{flags}'.encode())]))
        for result in self.wire():
            self.assertEqual(len(result.links), 1)
            self.assertLessEqual(result.links[0].flags & 255, 1)
        zero = TracerProvider(sampler=TraceIdRatioBased(0), shutdown_on_exit=False)
        zero.add_span_processor(SimpleSpanProcessor(self.memory))
        try:
            self.runtime.tracer = zero.get_tracer('zero')
            count = len(self.spans())
            await self.client.get('/' + SECRET, headers={'traceparent': f'00-{tid}-{sid}-ff'})
            self.assertEqual(len(self.spans()), count)
        finally:
            zero.shutdown()

    async def test_rebuild_config_span_tracks_only_backend_fetch(self):
        config_api = NS(get=lambda: None)
        client = NS(collections=NS(get=lambda name: NS(config=config_api)))
        hnsw = {'efConstruction': 64, 'maxConnections': 16, 'ef': 32}
        config = NS(vector_index_config=NS(ef_construction=64, max_connections=16, ef=32,
                                          distance_metric=wc.VectorDistances.COSINE),
                    properties=[], vectorizer=None, vectorizer_config=None)
        for fetch_fails in (False, True):
            with self.subTest(fetch_fails=fetch_fails):
                # Stop at staging creation, after the real fetch and local lookup.
                # This covers the producer without performing collection writes.
                failure = ValueError(SECRET) if fetch_fails else RuntimeError(SECRET)
                before = len(self.spans('weaviate.config'))
                with contextlib.ExitStack() as stack:
                    stack.enter_context(patch.object(wc, 'get_client', return_value=client))
                    fetch = stack.enter_context(patch.object(config_api, 'get',
                        return_value=config, side_effect=failure if fetch_fails else None))
                    stack.enter_context(patch.object(tuning.collection_recovery, 'begin',
                        return_value={'staging': 'Scratch', 'state': 'scratch'}))
                    stack.enter_context(patch.object(tuning.collection_recovery, 'discard'))
                    create = stack.enter_context(patch.object(wc, '_create_collection_sync', side_effect=failure))
                    stack.enter_context(ot.bind((self.runtime, None)))
                    with self.assertRaises(type(failure)) as caught:
                        tuning._rebuild('Synthetic', [], None, None, None)
                self.assertIs(caught.exception, failure)
                fetch.assert_called_once_with()
                if fetch_fails:
                    create.assert_not_called()
                else:
                    create.assert_called_once_with('Scratch', 'hnsw', 'cosine', hnsw, preserve_hnsw=True)
                emitted = self.spans('weaviate.config')[before:]
                self.assertEqual(len(emitted), 1)
                self.assertEqual(emitted[0].kind, trace.SpanKind.CLIENT)
                self.assertEqual(emitted[0].parent.span_id, self.spans('rag.rebuild')[-1].context.span_id)
                self.assertEqual(emitted[0].attributes['rag.outcome'], 'error' if fetch_fails else 'ok')
        self.wire()

    async def test_export_admission_concurrency_and_after_response(self):
        release = threading.Event()
        entered = []
        def build(collection, **kwargs):
            entered.append(trace.get_current_span().get_span_context().trace_id)
            if not release.wait(3):
                raise TimeoutError('test synchronization')
            ot.call('weaviate.query', lambda: None)
            return dict(filename=SECRET, size_bytes=1, chunk_count=1, source_document_count=0,
                        fidelity='chunks-only', models_bundled=False, retrieve_script=False, warnings=[])
        try:
            with patch.object(wc, 'collection_exists', AsyncMock(return_value=True)), patch.object(exporter.packager, 'build', build):
                a = await self.client.post('/export', json={'collection':'TraceA'})
                b = await self.client.post('/export', json={'collection':'TraceB'})
                self.assertEqual((a.status_code,b.status_code),(202,202))
                await self.until(lambda: len(entered)==2)
                self.assertFalse(self.spans('rag.export'))
                self.assertEqual(len(set(entered)),2)
                release.set()
                await self.until(lambda: len(self.spans('rag.export'))==2)
        finally:
            release.set()
        self.lineage('rag.export')
        request_ends = {s.context.span_id:s.end_time for s in self.spans('rag.request')}
        self.assertTrue(all(s.end_time > request_ends[s.parent.span_id] for s in self.spans('rag.export')))

    async def test_real_json_parser_keeps_parse_span_after_upstream_helpers(self):
        for suffix in ('.json', '.json.gz'):
            for valid in (True, False):
                with self.subTest(suffix=suffix, valid=valid):
                    self.memory.clear()
                    path = Path(self.temp.name) / (SECRET + suffix)
                    content = ('{"text": "' + SECRET + '"}' if valid else '{').encode()
                    path.write_bytes(gzip.compress(content) if suffix.endswith('.gz') else content)
                    with ot.bind((self.runtime, None)), ot.span('rag.ingest') as parent:
                        if valid:
                            text, _ = ingest._parse_file(path)
                            self.assertIn(SECRET, text)
                        else:
                            with self.assertRaises(ValueError):
                                ingest._parse_file(path)
                    spans = self.spans('rag.parse')
                    self.assertEqual(len(spans), 1)
                    self.assertEqual(spans[0].parent.span_id, parent.get_span_context().span_id)
                    self.assertEqual(spans[0].status.status_code, trace.StatusCode.UNSET if valid else trace.StatusCode.ERROR)
                    self.wire()

    async def test_ingest_real_executor_partial_failure(self):
        def parse(path):
            if path.name.startswith('bad'):
                raise ValueError(SECRET)
            return SECRET, []
        with patch.object(wc,'collection_exists',AsyncMock(return_value=True)), patch.object(ingest,'_parse_file',ot.traced('rag.parse')(parse)), patch.object(ingest,'do_chunk',return_value=[SECRET]), patch.object(wc,'_insert_chunks_sync',side_effect=lambda *a: ot.call('weaviate.batch',lambda: None)), patch.object(ingest.sources,'store'):
            response = await self.client.post('/ingest/upload', data={'collection':'TraceIngest'}, files=[('files',('good.txt',b'data')),('files',('bad.txt',b'data'))])
            self.assertEqual(response.status_code,202,response.text)
            job_id=response.json()['job_id']
            await self.until(lambda: ingest.get_job(job_id)['status'] in ('partial','failed','completed'))
            await self.until(lambda: bool(self.spans('rag.ingest')))
        self.assertEqual(ingest.get_job(job_id)['status'],'partial')
        self.assertEqual(self.spans('rag.ingest')[0].attributes['rag.outcome'],'partial')
        self.lineage('rag.ingest')

    async def test_import_and_tuning_real_launchers_handled_failure(self):
        with patch.object(importer.packager,'open_package',side_effect=importer.PackageError('INVALID',SECRET)), patch.object(wc,'collection_exists',AsyncMock(return_value=True)), patch.object(tuning,'_existing_chunks',side_effect=ValueError(SECRET)):
            a=await self.client.post('/import',json={'filename':SECRET+'.tar.gz','on_conflict':'abort'})
            b=await self.client.post('/tune/reembed',json={'collection':'TraceTune'})
            self.assertEqual((a.status_code,b.status_code),(202,202))
            await self.until(lambda: bool(self.spans('rag.import')) and bool(self.spans('rag.tuning')))
        self.assertEqual(importer.get_job(a.json()['job_id'])['status'],'failed')
        self.assertEqual(tuning.get_job(b.json()['job_id'])['status'],'failed')
        self.lineage('rag.import'); self.lineage('rag.tuning')
        for name in ('rag.import','rag.tuning'):
            self.assertEqual(self.spans(name)[0].attributes['rag.outcome'],'error')

    async def test_generation_real_task_and_regeneration_timeout(self):
        session={'session_id':'synthetic','status':'generating','pairs':[], 'pairs_total':1,'pairs_completed':0,'pairs_failed':0,'pairs_attempted':0}
        def update(sid,change):
            return change(session)
        ready=asyncio.Event()
        async def model(*args):
            await ready.wait()
            return '{"question":"synthetic","answer":"synthetic","ground_truth":"synthetic"}'
        with patch.object(wc,'collection_exists',AsyncMock(return_value=True)), patch.object(gs,'_prepare_generation_sync',return_value=(session,[{'content':SECRET}])), patch.object(gs,'_update_session_sync',update), patch.object(ollama,'chat',ot.traced('ollama.chat')(model)):
            response=await self.client.post('/goldstandard/generate',json={'collection':'TraceGold','sample_size':1})
            self.assertEqual(response.status_code,202,response.text)
            ready.set()
            await self.until(lambda: bool(self.spans('rag.evaluation')))
        self.assertEqual(session['status'],'completed')
        self.lineage('rag.evaluation')
        pair={'pair_id':'pair','contexts':[SECRET],'source_file':SECRET,'chunk_index':0}
        async def slow(*args):
            await asyncio.sleep(10)
        with patch.object(gs,'get_session',return_value={'pairs':[pair],'status':'completed'}), patch.object(gs,'_REGENERATION_TIMEOUT_SECONDS',.005), patch.object(ollama,'chat',ot.traced('ollama.chat')(slow)):
            response=await self.client.post('/goldstandard/regenerate',json={'session_id':'session','pair_id':'pair'})
        self.assertEqual(response.status_code,504,response.text)
        self.assertEqual(self.spans('rag.regenerate')[-1].attributes['error.type'],'timeout')
        self.assertTrue(any(s.attributes.get('rag.outcome')=='cancelled' for s in self.spans('rag.pair')))
        self.wire()

    async def test_thread_waiter_cancel_and_context_restore(self):
        entered=threading.Event(); release=threading.Event()
        @ot.traced('rag.export')
        def work():
            entered.set();release.wait(3)
        with ot.bind((self.runtime,None)), ot.span('rag.request') as parent:
            captured=ot.admitted(work)
        task=asyncio.create_task(asyncio.to_thread(captured))
        await self.until(entered.is_set)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(self.spans('rag.export'))
        release.set()
        await self.until(lambda: bool(self.spans('rag.export')))
        self.assertEqual(self.spans('rag.export')[0].parent.span_id,parent.get_span_context().span_id)
        with ThreadPoolExecutor(max_workers=1) as pool:
            await asyncio.get_running_loop().run_in_executor(pool,captured)
            context=await asyncio.get_running_loop().run_in_executor(pool,ot.capture)
        self.assertEqual(context,(None,None))

    async def test_iterator_batch_boundaries_and_no_context_leak(self):
        def rows():
            yield 1
            raise ConnectionError(SECRET)
        with ot.bind((self.runtime,None)),ot.span('rag.export') as parent:
            iterator=ot.iterate(rows)
            self.assertEqual(next(iterator),1)
            self.assertEqual(trace.get_current_span(),parent)
            with self.assertRaises(ConnectionError):next(iterator)
        self.assertEqual(self.spans('weaviate.iterate')[0].attributes['error.type'],'connection')
        self.wire()

    async def test_telemetry_fault_and_disabled_do_not_repeat_work(self):
        for tracer in (None, NS(start_span=lambda *a,**k: (_ for _ in ()).throw(RuntimeError(SECRET)))):
            self.runtime.tracer=tracer
            calls=[]
            with ot.bind((self.runtime,None)):
                self.assertEqual(ot.call('weaviate.query',lambda: calls.append(1) or 7),7)
                self.assertEqual(list(ot.iterate(lambda:iter([1,2]))),[1,2])
            self.assertEqual(calls,[1])
        self.assertEqual(ot.capture(),(None,None))

    async def test_generation_cancellation_and_failure_reporter(self):
        session={'session_id':'cancel-session','status':'generating','pairs':[], 'pairs_total':1,'pairs_completed':0,'pairs_failed':0,'pairs_attempted':0}
        def update(sid,change): return change(session)
        entered=asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.sleep(10)
        with patch.object(wc,'collection_exists',AsyncMock(return_value=True)), patch.object(gs,'_prepare_generation_sync',return_value=(session,[{'content':SECRET}])), patch.object(gs,'_update_session_sync',update), patch.object(ollama,'chat',ot.traced('ollama.chat')(slow)):
            response=await self.client.post('/goldstandard/generate',json={'collection':'TraceCancel','sample_size':1})
            self.assertEqual(response.status_code,202)
            await entered.wait()
            tasks=list(gs._tasks)
            for task in tasks: task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
        self.assertEqual(session['status'],'cancelled')
        self.assertEqual(self.spans('rag.evaluation')[-1].attributes['rag.outcome'],'cancelled')
        # Real done callback creates the failure reporter with captured origin.
        session['status']='generating'
        async def model(*args): return '{"question":"q","answer":"a","ground_truth":"g"}'
        with patch.object(wc,'collection_exists',AsyncMock(return_value=True)), patch.object(gs,'_prepare_generation_sync',return_value=(session,[{'content':SECRET}])), patch.object(gs,'_update_session_sync',side_effect=gs.GoldStandardError('STORE_FAILED',SECRET,500)), patch.object(gs,'_publish_generation_failure'), patch.object(ollama,'chat',ot.traced('ollama.chat')(model)):
            await self.client.post('/goldstandard/generate',json={'collection':'TraceFailed','sample_size':1})
            await self.until(lambda: bool(self.spans('rag.failure_report')))
        self.lineage('rag.failure_report')
        self.assertEqual(self.spans('rag.failure_report')[-1].attributes['rag.outcome'],'error')

    async def test_ollama_real_client_error_and_nested_asyncio_run(self):
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def post(self,*args,**kwargs): raise httpx.ReadTimeout(SECRET)
            async def get(self,*args,**kwargs): raise httpx.ConnectError(SECRET)
        with patch.object(ollama.httpx,'AsyncClient',return_value=Client()), ot.bind((self.runtime,None)), ot.span('rag.import') as parent:
            # This is the actual importer's synchronous bridge into a fresh loop.
            result=await asyncio.to_thread(importer._probe_dimensions)
            self.assertIsNone(result)
            health=await ollama.check_health()
        self.assertEqual(health['llm']['status'],'error')
        self.assertEqual(self.spans('ollama.embed')[-1].parent.span_id,parent.get_span_context().span_id)
        self.assertEqual(self.spans('ollama.embed')[-1].attributes['error.type'],'timeout')
        self.assertEqual(self.spans('ollama.health')[-1].attributes['error.type'],'connection')
        self.wire()

    async def test_streaming_request_cancellation_and_validation(self):
        from fastapi.responses import StreamingResponse
        entered=asyncio.Event()
        @self.app.get('/stream')
        async def stream():
            async def chunks():
                yield b'first'
                entered.set()
                await asyncio.sleep(10)
            return StreamingResponse(chunks())
        task=asyncio.create_task(self.client.get('/stream'))
        await entered.wait()
        self.assertFalse(self.spans('rag.request'))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        self.assertEqual(self.spans('rag.request')[-1].attributes['rag.outcome'],'cancelled')
        response=await self.client.post('/query',json={'question':SECRET})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.spans('rag.request')[-1].attributes['http.status_class'],'4xx')
        self.wire()

    async def test_actual_batch_failure_and_export_iterator_paths(self):
        from services import batch_write, packager
        class Batch:
            number_errors=0
            def __enter__(self): return self
            def __exit__(self,*args):
                self.finished=True
            def add_object(self,**kwargs): pass
        batch=Batch()
        coll=NS(batch=NS(dynamic=lambda:batch,failed_objects=[NS(message=SECRET)]))
        records=[{'id':'00000000-0000-0000-0000-000000000001','properties':{'content':SECRET},'vector':[1.0]}]
        with ot.bind((self.runtime,None)),ot.span('rag.ingest'),self.assertRaises(RuntimeError):
            batch_write.insert(coll,records)
        self.assertTrue(batch.finished)
        self.assertEqual(self.spans('weaviate.batch')[-1].attributes['rag.outcome'],'error')
        obj=NS(uuid='00000000-0000-0000-0000-000000000001',properties={'content':SECRET,'source_file':SECRET},vector=[1.0])
        coll=NS(iterator=lambda **kw:iter([obj]))
        with patch.object(wc,'get_client',return_value=NS(collections=NS(get=lambda name:coll))),patch.object(packager.sources,'load_index',return_value={'documents':{}}),ot.bind((self.runtime,None)),ot.span('rag.export') as parent:
            self.assertEqual(len(list(packager.read_chunks('Synthetic'))),1)
            self.assertEqual(trace.get_current_span(),parent)
        self.assertTrue(self.spans('weaviate.iterate'))
        self.wire()

    async def test_real_export_link_sanitization_both_boundaries(self):
        from test_telemetry import receiver
        from opentelemetry.trace import Link, SpanContext, TraceFlags, TraceState
        with receiver() as (env,records):
            env.update(RAG_OTEL_LOGS='false',RAG_OTEL_METRICS='false')
            runtime=ot.bootstrap(env)
            try:
                context=SpanContext(1,2,True,TraceFlags(1),TraceState([('private',SECRET)]))
                with ot.bind((runtime,None)),ot.span('rag.request',links=[Link(context,{'secret':SECRET}),Link(context)]):
                    pass
                self.assertTrue(runtime.force_flush())
            finally:
                runtime.shutdown()
        traces=[payload for path,payload in records if path=='/v1/traces']
        self.assertTrue(traces)
        for payload in traces:
            self.assertNotIn(SECRET.encode(),payload)
            spans=ExportTraceServiceRequest.FromString(payload).resource_spans[0].scope_spans[0].spans
            self.assertEqual(len(spans[0].links),1)
            self.assertEqual(spans[0].links[0].trace_id,b'\x00'*15+b'\x01')
            self.assertEqual(spans[0].links[0].trace_state,'')
            self.assertFalse(spans[0].links[0].attributes)

if __name__=='__main__':
    unittest.main()
