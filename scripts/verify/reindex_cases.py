"""Controlled reindex preservation/failure cases; registered by14_reindex.sh."""
import copy, math, os, sys, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or (str(Path(__file__).resolve().parents[2]/'api') if __file__ != '<stdin>' else '/app'))
from services import tuning
validate_vectorizer = tuning.wc._validate_reindex_vectorizer_sync


def records():
    return [{'id': '49000000-0000-4000-8000-00000000000'+str(i),
             'vector': [0.125, float(i), -0.25],
             'properties': {'content': 'Owned inert '+str(i), 'chunk_index': i, 'source_file': 'owned.txt'}} for i in range(2)]


class Batch:
    def __init__(self, owner, name):
        self.owner, self.name, self.number_errors, self.pending = owner, name, 0, []
    def __enter__(self): return self
    def add_object(self, properties, uuid=None, vector=None):
        self.pending.append({'id': str(uuid), 'vector': copy.deepcopy(vector), 'properties': copy.deepcopy(properties)})
        if self.owner.mutate_arguments:
            properties['content'] = 'SDK argument mutation'; vector[0] = 999
    def __exit__(self, *exc):
        if self.owner.fail(self.name): self.number_errors = 1
        else: self.owner.data[self.name] += self.pending
        self.owner.closed.append(self.name)
        if self.owner.corrupt(self.name) and self.owner.data[self.name]:
            self.owner.data[self.name][0]['properties']['content'] = 'Owned readback corruption'
        if self.owner.change_source and self.name != 'OwnedReindex':
            self.owner.data['OwnedReindex'][0]['properties']['content'] = 'Newer independent write'


class Collections:
    def __init__(self, initial):
        self.data = {'OwnedReindex': copy.deepcopy(initial)}; self.deleted = []; self.created = []; self.closed = []
        self.fail = lambda name: False; self.corrupt = lambda name: False
        self.change_source = self.mutate_arguments = False
    def get(self, name):
        def iterator(include_vector=False):
            for record in self.data[name]:
                yield SimpleNamespace(uuid=record['id'], properties=copy.deepcopy(record['properties']), vector={'default':copy.deepcopy(record['vector'])} if include_vector else None)
        return SimpleNamespace(iterator=iterator, batch=SimpleNamespace(dynamic=lambda: Batch(self, name), failed_objects=[]), aggregate=SimpleNamespace(over_all=lambda **kwargs: SimpleNamespace(total_count=len(self.data[name]))))
    def delete(self, name): self.deleted.append(name); del self.data[name]
    def create(self, name, index, distance, hnsw, **kwargs):
        self.created.append((name,index,distance,copy.deepcopy(hnsw))); self.data[name] = []


class ReindexTests(unittest.TestCase):
    def setUp(self):
        self.original = records(); self.backend = Collections(self.original)
        self.config = {'index_type':'hnsw','distance_metric':'cosine','hnsw_config':{'ef':64,'efConstruction':128,'maxConnections':64}}
        self.embedding = Mock(side_effect=AssertionError('Reindex contacted embedding insertion'))
        self.stale = Mock(return_value=1)
        patches = [patch.object(tuning.wc,'get_client',return_value=SimpleNamespace(collections=self.backend)),
                   patch.object(tuning.wc,'_create_collection_sync',side_effect=self.backend.create),
                   patch.object(tuning.wc,'_collection_config_sync',return_value=self.config),
                   patch.object(tuning.wc,'_validate_reindex_vectorizer_sync'),
                   patch.object(tuning.wc,'_insert_chunks_sync',self.embedding),
                   patch.object(tuning.sources,'has_sources',return_value=False),
                   patch.object(tuning.goldstandard,'mark_stale',self.stale),
                   patch.object(tuning,'_jobs',{}),patch.object(tuning,'_active',{'OwnedReindex'})]
        for change in patches: change.start(); self.addCleanup(change.stop)
        import tempfile
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        def begin(collection,operation,client):return {'staging':collection+'__tuning_owned','state':'scratch','operation_id':'owned'}
        def retain(owner, **kwargs):owner['state']='recovery'
        def discard(owner,client):client.collections.delete(owner['staging'])
        for change in [patch.object(tuning.collection_recovery,'begin',side_effect=begin),patch.object(tuning.collection_recovery,'retain',side_effect=retain),patch.object(tuning.collection_recovery,'discard',side_effect=discard),patch.object(tuning.collection_recovery,'_root',return_value=Path(self.temp.name))]:
            change.start();self.addCleanup(change.stop)
    def run_job(self, operation='reindex'):
        tuning._jobs['owned'] = {'status':'queued','chunks_written':0,'notes':[]}
        tuning._run('owned','OwnedReindex',operation,{'index_type':'flat','distance_metric':'dot',**({'chunking':{}} if operation=='rechunk' else {})})
        return tuning._jobs['owned']
    def test_reindex_changes_physical_config_and_preserves_every_record_without_embedding(self):
        job=self.run_job(); self.assertEqual(job['status'],'completed'); self.assertEqual(job['chunks_written'],2)
        self.assertEqual(self.backend.data['OwnedReindex'],self.original); self.embedding.assert_not_called(); self.stale.assert_not_called()
        self.assertTrue(all(entry[1:3]==('flat','dot') for entry in self.backend.created)); self.assertIn('verified unchanged',job['notes'][0])
        self.assertNotIn('OwnedReindex',tuning._active)
    def test_deferred_batch_failure_is_seen_before_original_deletion(self):
        self.backend.fail=lambda name:name!='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertNotIn('OwnedReindex',self.backend.deleted)
        self.assertEqual(self.backend.data['OwnedReindex'],self.original); self.assertEqual(job['chunks_written'],0); self.stale.assert_not_called()
    def test_staging_readback_mismatch_preserves_original(self):
        self.backend.corrupt=lambda name:name!='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertNotIn('OwnedReindex',self.backend.deleted); self.stale.assert_not_called()
    def test_observed_source_change_during_staging_is_not_overwritten(self):
        self.backend.change_source=True; job=self.run_job(); self.assertEqual(job['status'],'failed')
        self.assertNotIn('OwnedReindex',self.backend.deleted); self.assertEqual(self.backend.data['OwnedReindex'][0]['properties']['content'],'Newer independent write')
    def test_final_deferred_failure_has_no_completion_claim_and_marks_retained_pairs(self):
        self.backend.fail=lambda name:name=='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(job['chunks_written'],0); self.assertEqual(job['notes'],[])
        self.stale.assert_called_once(); self.assertIn('cutover',self.stale.call_args.args[1])
    def test_final_readback_mismatch_is_not_completed(self):
        self.backend.corrupt=lambda name:name=='OwnedReindex'; job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(job['chunks_written'],0); self.stale.assert_called_once()
    def test_missing_or_nonfinite_vectors_fail_before_any_creation(self):
        for vector in [[],None,[math.nan],[math.inf],[True]]:
            with self.subTest(vector=vector):
                self.backend.data['OwnedReindex'][0]['vector']=vector; job=self.run_job()
                self.assertEqual(job['status'],'failed'); self.assertEqual(self.backend.created,[]); self.assertEqual(self.backend.deleted,[])
    def test_unsupported_named_vector_is_refused_without_guessing(self):
        col=SimpleNamespace(iterator=lambda **kw:iter([SimpleNamespace(uuid=self.original[0]['id'],properties={},vector={'other':[1.0]})]))
        with patch.object(tuning.wc,'get_client',return_value=SimpleNamespace(collections=SimpleNamespace(get=lambda name:col))):
            with self.assertRaisesRegex(RuntimeError,'single default vector'): tuning._existing_records('OwnedReindex')
    def test_duplicate_readback_id_is_refused(self):
        self.backend.data['OwnedReindex'].append(copy.deepcopy(self.original[0])); job=self.run_job()
        self.assertEqual(job['status'],'failed'); self.assertEqual(self.backend.created,[])
    def test_sdk_argument_mutation_does_not_change_expected_snapshot(self):
        self.backend.mutate_arguments=True; snapshot=tuning._existing_records('OwnedReindex'); before=copy.deepcopy(snapshot)
        self.backend.create('OwnedCopy','flat','dot',{}); tuning._write_records('OwnedCopy',snapshot)
        self.assertEqual(snapshot,before); self.assertEqual(self.backend.data['OwnedCopy'],self.original)
    def test_empty_collection_reindexes_without_embedding(self):
        self.backend.data['OwnedReindex']=[]; job=self.run_job()
        self.assertEqual(job['status'],'completed'); self.assertEqual(job['chunks_written'],0); self.embedding.assert_not_called(); self.stale.assert_not_called()
    def test_ingest_started_after_source_check_waits_for_final_reindex_copy(self):
        import tempfile,threading,uuid
        from concurrent.futures import ThreadPoolExecutor
        from services import ingest_pipeline as ingest
        entered=threading.Event();release=threading.Event();parsed=threading.Event();attempted=threading.Event()
        verify=tuning._verify_records;paused=False
        def paused_verify(name,records):
            nonlocal paused
            verify(name,records)
            if name=='OwnedReindex' and not paused:
                paused=True;entered.set();assert release.wait(3)
        def parse(path):parsed.set();return 'Owned later upload',[]
        def insert(name,chunks):self.backend.data[name].append({'id':str(uuid.uuid4()),'vector':[.125,0.,-.25],'properties':chunks[0]})
        job={'status':'queued','chunks_stored':0,'files_completed':0,'files_failed':0,'files_total':1,'errors':[]}
        with tempfile.TemporaryDirectory() as temp,patch.object(tuning,'_verify_records',side_effect=paused_verify),patch.object(ingest,'_jobs',{'owned_ingest':job}),patch.object(ingest,'_parse_file',side_effect=parse),patch.object(ingest,'do_chunk',return_value=['Owned later upload']),patch.object(tuning.wc,'_insert_chunks_sync',side_effect=insert),patch.object(tuning.wc,'_source_has_chunks_sync',return_value=False),patch.object(ingest.sources,'store'),ThreadPoolExecutor() as pool:
            path=Path(temp)/'owned.txt';path.write_text('Owned later upload')
            reindex=pool.submit(self.run_job);self.assertTrue(entered.wait(2))
            def start_ingest():
                attempted.set();ingest._process_job_sync('owned_ingest',[path],Path(temp),'OwnedReindex','fixed',150,0,.5,40)
            upload=pool.submit(start_ingest);self.assertTrue(attempted.wait(2));self.assertFalse(parsed.wait(.05));release.set()
            result=reindex.result(timeout=3);upload.result(timeout=3)
        self.assertEqual(result['status'],'completed');self.assertEqual(job['status'],'completed')
        self.assertEqual(self.backend.data['OwnedReindex'][:2],self.original);self.assertEqual(len(self.backend.data['OwnedReindex']),3)
    def test_reindex_snapshot_waits_for_already_running_ingest(self):
        import tempfile,threading,uuid
        from concurrent.futures import ThreadPoolExecutor
        from services import ingest_pipeline as ingest
        parsed=threading.Event();release=threading.Event();snapshot=threading.Event();attempted=threading.Event()
        original_read=tuning._existing_records
        def read(name):snapshot.set();return original_read(name)
        def parse(path):parsed.set();assert release.wait(3);return 'Owned active upload',[]
        def insert(name,chunks):self.backend.data[name].append({'id':str(uuid.uuid4()),'vector':[.125,0.,-.25],'properties':chunks[0]})
        job={'status':'queued','chunks_stored':0,'files_completed':0,'files_failed':0,'files_total':1,'errors':[]}
        with tempfile.TemporaryDirectory() as temp,patch.object(tuning,'_existing_records',side_effect=read),patch.object(ingest,'_jobs',{'owned_ingest':job}),patch.object(ingest,'_parse_file',side_effect=parse),patch.object(ingest,'do_chunk',return_value=['Owned active upload']),patch.object(tuning.wc,'_insert_chunks_sync',side_effect=insert),patch.object(tuning.wc,'_source_has_chunks_sync',return_value=False),patch.object(ingest.sources,'store'),ThreadPoolExecutor() as pool:
            path=Path(temp)/'owned.txt';path.write_text('Owned active upload')
            upload=pool.submit(ingest._process_job_sync,'owned_ingest',[path],Path(temp),'OwnedReindex','fixed',150,0,.5,40)
            self.assertTrue(parsed.wait(2))
            def start_reindex():attempted.set();return self.run_job()
            reindex=pool.submit(start_reindex);self.assertTrue(attempted.wait(2));self.assertFalse(snapshot.wait(.05));release.set()
            upload.result(timeout=3);result=reindex.result(timeout=3)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['chunks_written'],3)
        self.assertEqual(self.backend.data['OwnedReindex'][:2],self.original);self.assertEqual(len(self.backend.data['OwnedReindex']),3)
    def test_foreign_vectorizer_refuses_before_staging_or_delete(self):
        tuning.wc._validate_reindex_vectorizer_sync.side_effect=ValueError('Owned incompatible model')
        job=self.run_job();self.assertEqual(job['status'],'failed')
        self.assertEqual(self.backend.created,[]);self.assertEqual(self.backend.deleted,[])
        self.assertEqual(self.backend.data['OwnedReindex'],self.original);self.stale.assert_not_called()
    def test_vectorizer_validation_checks_model_endpoint_type_and_named_vectors(self):
        from config import settings
        cfg=SimpleNamespace(vectorizer_config=SimpleNamespace(vectorizer='text2vec-ollama',model={'model':settings.embed_model,'apiEndpoint':f'http://{settings.ollama_host}:{settings.ollama_port}'},vectorize_collection_name=False),vector_config=None,properties=[SimpleNamespace(name=p.name,data_type=p._to_dict()["dataType"][0],vectorizer='text2vec-ollama',vectorizer_config=SimpleNamespace(skip=False,vectorize_property_name=True),vectorizer_configs=None,nested_properties=None) for p in tuning.wc.COLLECTION_PROPERTIES])
        client=SimpleNamespace(collections=SimpleNamespace(get=lambda name:SimpleNamespace(config=SimpleNamespace(get=lambda:cfg))))
        with patch.object(tuning.wc,'get_client',return_value=client):
            validate_vectorizer('Owned')
            for attribute,value in [('model','foreign'),('apiEndpoint','http://foreign:1')]:
                original=cfg.vectorizer_config.model[attribute];cfg.vectorizer_config.model[attribute]=value
                with self.assertRaises(ValueError):validate_vectorizer('Owned')
                cfg.vectorizer_config.model[attribute]=original
            cfg.vectorizer_config.vectorizer='unsupported'
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
            cfg.vectorizer_config.vectorizer='text2vec-ollama';cfg.vector_config={'foreign':object()}
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
    def test_vectorizer_refuses_extra_module_options_and_changed_property_inputs(self):
        from config import settings
        props=[SimpleNamespace(name=p.name,data_type=p._to_dict()["dataType"][0],vectorizer='text2vec-ollama',vectorizer_config=SimpleNamespace(skip=False,vectorize_property_name=True),vectorizer_configs=None,nested_properties=None) for p in tuning.wc.COLLECTION_PROPERTIES]
        cfg=SimpleNamespace(vectorizer_config=SimpleNamespace(vectorizer='text2vec-ollama',model={'model':settings.embed_model,'apiEndpoint':f'http://{settings.ollama_host}:{settings.ollama_port}'},vectorize_collection_name=False),vector_config=None,properties=props)
        client=SimpleNamespace(collections=SimpleNamespace(get=lambda name:SimpleNamespace(config=SimpleNamespace(get=lambda:cfg))))
        with patch.object(tuning.wc,'get_client',return_value=client):
            validate_vectorizer('Owned')
            cfg.vectorizer_config.model['source_properties']=['content']
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
            cfg.vectorizer_config.model.pop('source_properties')
            for field,value in [('skip',True),('vectorize_property_name',False)]:
                rules=props[0].vectorizer_config;old=getattr(rules,field);setattr(rules,field,value)
                with self.assertRaises(ValueError):validate_vectorizer('Owned')
                setattr(rules,field,old)
            for field,value in [('name','custom_text'),('data_type','int'),('vectorizer','foreign'),('vectorizer_configs',{'default':object()})]:
                old=getattr(props[0],field);setattr(props[0],field,value)
                with self.assertRaises(ValueError):validate_vectorizer('Owned')
                setattr(props[0],field,old)
            props[1]=props[0]
            with self.assertRaises(ValueError):validate_vectorizer('Owned')
    def test_staging_creation_failures_cleanup_owned_scratch_without_touching_original(self):
        create=self.backend.create
        for created_before_error in (False,True):
            with self.subTest(created_before_error=created_before_error):
                def fail(name,*args):
                    if created_before_error:create(name,*args)
                    raise RuntimeError('Owned staging create acknowledgement failure')
                def discard(owner,client):
                    if owner['staging'] in client.collections.data:client.collections.delete(owner['staging'])
                with patch.object(tuning.wc,'_create_collection_sync',side_effect=fail),patch.object(tuning.collection_recovery,'discard',side_effect=discard) as cleanup:
                    job=self.run_job()
                self.assertEqual(job['status'],'failed');cleanup.assert_called_once()
                self.assertEqual(set(self.backend.data),{'OwnedReindex'});self.assertEqual(self.backend.data['OwnedReindex'],self.original)
                self.stale.assert_not_called()
    def test_delete_refusal_with_intact_original_preserves_evaluation_validity(self):
        delete=self.backend.delete
        def refuse(name):
            if name=='OwnedReindex':raise RuntimeError('Owned delete refusal')
            delete(name)
        with patch.object(self.backend,'delete',side_effect=refuse):job=self.run_job()
        self.assertEqual(job['status'],'failed');self.assertEqual(self.backend.data['OwnedReindex'],self.original)
        self.stale.assert_not_called();self.assertEqual(set(self.backend.data),{'OwnedReindex'})
    def test_uncertain_delete_retains_recovery_and_marks_historical(self):
        delete=self.backend.delete
        def uncertain(name):
            delete(name)
            if name=='OwnedReindex':raise RuntimeError('Owned lost delete acknowledgement')
        with patch.object(self.backend,'delete',side_effect=uncertain):job=self.run_job()
        self.assertEqual(job['status'],'failed');self.stale.assert_called_once()
        retained=job['error_detail']['recovered_as'];self.assertEqual(self.backend.data[retained],self.original)
        self.assertEqual(job['chunks_written'],0)
    def test_lowercase_alias_uses_canonical_job_and_cutover_identity(self):
        tuning._jobs['owned']={'status':'queued','chunks_written':0,'notes':[]}
        tuning._run('owned','ownedReindex','reindex',{'index_type':'flat','distance_metric':'dot'})
        job=tuning._jobs['owned'];self.assertEqual(job['status'],'completed');self.assertEqual(job['collection'],'OwnedReindex')
        self.assertEqual(self.backend.data['OwnedReindex'],self.original)
        self.assertTrue(all(name.startswith('OwnedReindex') for name,_,_,_ in self.backend.created))
    def test_alias_jobs_share_the_same_active_identity(self):
        import asyncio
        from services import jobstore
        async def check():
            # The start stores its record through JobStore.create() in a thread
            # (#309), so asyncio.to_thread stays real; the worker is stubbed.
            with patch.object(tuning,'_active',set()),patch.object(tuning,'_jobs',jobstore.JobStore('tune')),patch.object(tuning,'_run'):
                identity=await tuning.start_tune_job('ownedReindex','reindex',{})
                self.assertEqual(tuning._jobs[identity]['collection'],'OwnedReindex')
                with self.assertRaises(RuntimeError):await tuning.start_tune_job('OwnedReindex','reindex',{})
                await asyncio.sleep(0)
        asyncio.run(check())
    def test_rechunk_and_reembed_preserve_caller_spelled_source_identity(self):
        for operation in ('rechunk','reembed'):
            with self.subTest(operation=operation),patch.object(tuning.sources,'has_sources',return_value=True) as has_sources,patch.object(tuning,'_chunks_from_sources',return_value=[{'content':'Owned caller sources'}]) as read_sources,patch.object(tuning,'_existing_chunks',return_value=[{'content':'Owned caller chunks'}]),patch.object(tuning,'_rebuild',return_value=1):
                tuning._jobs['owned']={'status':'queued','chunks_written':0,'notes':[]}
                params={'chunking':{'strategy':'fixed','chunk_size':150,'chunk_overlap':0,'similarity_threshold':.5,'min_chunk_size':40}}
                tuning._run('owned','ownedReindex',operation,params)
                self.assertEqual(tuning._jobs['owned']['status'],'completed');has_sources.assert_called_once_with('ownedReindex');self.assertEqual(read_sources.call_args.args[0],'ownedReindex')
    def test_every_tuning_operation_registers_owned_staging(self):
        for operation in ('reindex','reembed','rechunk'):
            with self.subTest(operation=operation),patch.object(tuning.collection_recovery,'begin',wraps=tuning.collection_recovery.begin) as begin:
                # Existing controlled rebuild fixture executes the real rebuild;
                # rechunk uses inert parsed properties without changing traversal.
                with patch.object(tuning.sources,'has_sources',return_value=True),patch.object(tuning,'_chunks_from_sources',return_value=[r['properties'] for r in self.original]),patch.object(tuning.wc,'_insert_chunks_sync',side_effect=lambda name,props:self.backend.data.__setitem__(name,__import__('copy').deepcopy(self.original))):
                    job=self.run_job(operation)
                begin.assert_called_once();self.assertEqual(begin.call_args.args[:2],('OwnedReindex','tune'))
                self.assertEqual(set(self.backend.data),{'OwnedReindex'})
    def test_reembed_retains_its_explicit_regeneration_path(self):
        def embed(name,props):
            self.backend.data[name]=[{'id':'49000000-0000-4000-8000-000000000100','vector':[4.,5.,6.],'properties':copy.deepcopy(props[0])},
                                     {'id':'49000000-0000-4000-8000-000000000101','vector':[4.,5.,6.],'properties':copy.deepcopy(props[1])}]
        self.embedding.side_effect=embed; job=self.run_job('reembed')
        self.assertEqual(job['status'],'completed'); self.embedding.assert_called_once(); self.stale.assert_called_once()
        self.assertNotEqual(self.backend.data['OwnedReindex'],self.original)

if __name__=='__main__': unittest.main()
