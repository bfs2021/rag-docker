"""Writer barriers cover complete workers, nesting and independent collections."""
import os,sys,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR') or (str(Path(__file__).resolve().parents[2]/'api') if __file__!='<stdin>' else '/app'))
from services import collection_writes as writes

class WriterTests(unittest.TestCase):
    def test_aliases_preserve_other_case_and_remove_duplicates(self):
        self.assertEqual(writes.aliases('ownedCorpus'), ('OwnedCorpus', 'ownedCorpus'))
        self.assertEqual(writes.aliases('OwnedCorpus'), ('OwnedCorpus', 'ownedCorpus'))
        self.assertNotEqual(writes.aliases('OwnedCorpus'), writes.aliases('Ownedcorpus'))
        self.assertEqual(writes.aliases(''), ('',))

    def test_same_collection_waits_until_complete_guard_releases(self):
        attempted=threading.Event();entered=threading.Event()
        def worker():
            attempted.set()
            with writes.guard('Owned'):entered.set()
        with ThreadPoolExecutor() as pool:
            with writes.guard('Owned'):
                task=pool.submit(worker);self.assertTrue(attempted.wait(2));self.assertFalse(entered.wait(.05))
            task.result(timeout=2);self.assertTrue(entered.is_set())
        self.assertNotIn('Owned',writes._registry)
    def test_reentrant_worker_and_primitive_share_one_guard(self):
        @writes.serialized('collection')
        def primitive(collection):
            with writes.guard(collection):return writes._registry[collection][1]
        with writes.guard('Owned'):self.assertEqual(primitive(collection='Owned'),3)
        self.assertNotIn('Owned',writes._registry)
    def test_other_collections_do_not_wait(self):
        with ThreadPoolExecutor() as pool:
            with writes.guard('Owned'):
                def independent():
                    with writes.guard('Other'):return True
                self.assertTrue(pool.submit(independent).result(timeout=2))
    def test_failed_worker_releases_guard(self):
        with self.assertRaises(RuntimeError):
            with writes.guard('Owned'):raise RuntimeError('Owned failure')
        self.assertNotIn('Owned',writes._registry)
    def test_backend_case_aliases_share_the_same_guard(self):
        entered=threading.Event()
        def worker():
            with writes.guard('owned'):entered.set()
        with ThreadPoolExecutor() as pool:
            with writes.guard('Owned'):
                task=pool.submit(worker);self.assertFalse(entered.wait(.05))
            task.result(timeout=2);self.assertTrue(entered.is_set())
    def test_actual_import_and_backend_mutators_wait_before_entering_their_body(self):
        from unittest.mock import patch
        from services import importer,weaviate_client as wc
        operations=[(wc._create_collection_sync,('owned','hnsw','cosine',{}),wc,'get_client'),
                    (wc._insert_chunks_sync,('owned',[]),wc,'get_client'),
                    (wc._delete_collection_sync,('owned',),wc,'get_client'),
                    (importer._build,('owned',Path('/inert'),{},None),importer,'_create_from_package')]
        for operation,args,module,boundary in operations:
            with self.subTest(operation=operation.__name__):
                attempted=threading.Event();entered=threading.Event()
                def boundary_call(*args,**kwargs):
                    entered.set();raise RuntimeError('Owned stopped backend boundary')
                def worker():attempted.set();return operation(*args)
                with patch.object(module,boundary,side_effect=boundary_call),ThreadPoolExecutor() as pool:
                    with writes.guard('Owned'):
                        task=pool.submit(worker);self.assertTrue(attempted.wait(2));self.assertFalse(entered.wait(.05))
                    with self.assertRaisesRegex(RuntimeError,'Owned stopped'):task.result(timeout=2)
                    self.assertTrue(entered.is_set())
                self.assertNotIn('Owned',writes._registry)

class ImportCutoverTests(unittest.TestCase):
    def execute(self,build_hook=None,restore_hook=None,delete_hook=None):
        import tempfile,json
        from contextlib import ExitStack
        from unittest.mock import patch
        from types import SimpleNamespace
        from services import importer,collection_recovery as recovery
        from config import settings
        task=tempfile.TemporaryDirectory();self.addCleanup(task.cleanup);root=Path(task.name)
        pkg=root/'pkg';pkg.mkdir();(pkg/'manifest.json').write_text('{}')
        backend={'OwnedImport'};deleted=[];observed=[]
        class Collections:
            def exists(self,name):return name in backend
            def delete(self,name):deleted.append(name);backend.discard(name)
        client=SimpleNamespace(collections=Collections())
        job={'status':'queued','chunks_written':0};manifest={'collection':{'name':'OwnedImport','chunk_count':1}}
        def build(name,*args):
            backend.add(name)
            if name!='OwnedImport':
                records=[json.loads(p.read_text()) for p in recovery._root().glob('*.json')]
                self.assertEqual(len(records),1);self.assertEqual(records[0]['staging'],name);self.assertEqual(records[0]['state'],'scratch');observed.append(name)
            if build_hook:build_hook(name,backend)
            return 1
        def delete(name):
            client.collections.delete(name)
            if delete_hook:delete_hook(name)
        stack=ExitStack();self.addCleanup(stack.close)
        for change in [patch.object(settings,'upload_dir',str(root)),patch.object(settings,'sources_dir',str(root/'sources')),patch.object(importer,'_jobs',{'owned':job}),patch.object(importer,'_active',{'owned.zip'}),patch.object(importer.packager,'exports_dir',return_value=root),patch.object(importer.packager,'open_package',return_value=(pkg,manifest)),patch.object(importer.packager,'verify_digests'),patch.object(importer,'_check_embedding'),patch.object(importer,'_ensure_models',return_value=[]),patch.object(importer.wc,'get_client',return_value=client),patch.object(importer.wc,'_collection_exists_sync',side_effect=lambda name:name in backend),patch.object(importer.wc,'_delete_collection_sync',side_effect=delete),patch.object(importer,'_build',side_effect=build),patch.object(importer,'_mark_started'),patch.object(importer,'_mark_finished'),patch.object(importer.goldstandard,'sessions_for',return_value=[]),patch.object(importer,'_restore_sidecars',side_effect=restore_hook or (lambda *args:[]))]:stack.enter_context(change)
        return importer,job,backend,deleted,observed,root
    def test_replace_guard_spans_deleted_target_and_sidecar_restoration(self):
        from concurrent.futures import ThreadPoolExecutor
        deleted_event=threading.Event();restore_event=threading.Event();resume_delete=threading.Event();resume_restore=threading.Event();entered=threading.Event();attempted=threading.Event()
        def delete(name):deleted_event.set();assert resume_delete.wait(3)
        def restore(*args):restore_event.set();assert resume_restore.wait(3);return []
        module,job,backend,deleted,observed,root=self.execute(delete_hook=delete,restore_hook=restore)
        def writer():
            attempted.set()
            with writes.guard('ownedImport'):entered.set();self.assertIn('OwnedImport',backend)
        with ThreadPoolExecutor() as pool:
            task=pool.submit(module._run,'owned','owned.zip','replace');self.assertTrue(deleted_event.wait(2))
            waiting=pool.submit(writer);self.assertTrue(attempted.wait(2));self.assertFalse(entered.wait(.05));resume_delete.set()
            self.assertTrue(restore_event.wait(2));self.assertFalse(entered.wait(.05));resume_restore.set();task.result(timeout=3);waiting.result(timeout=3)
        self.assertEqual(job['status'],'completed');self.assertEqual(backend,{'OwnedImport'});self.assertEqual(len(observed),1)
        self.assertEqual(list((root/'collection_operations').glob('*.json')),[])
    def test_import_staging_failure_cleans_owned_journal_and_collection(self):
        def fail(name,backend):
            if name!='OwnedImport':raise RuntimeError('Owned staging insertion failed')
        module,job,backend,deleted,observed,root=self.execute(build_hook=fail)
        module._run('owned','owned.zip','replace');self.assertEqual(job['status'],'failed');self.assertEqual(backend,{'OwnedImport'})
        self.assertNotIn('OwnedImport',deleted);self.assertEqual(list((root/'collection_operations').glob('*.json')),[])
    def test_replace_failure_retains_positive_recovery_and_startup_preserves_it(self):
        from services import collection_recovery as recovery
        def fail(name,backend):
            if name=='OwnedImport':backend.remove(name);raise RuntimeError('Owned final insertion failed')
        module,job,backend,deleted,observed,root=self.execute(build_hook=fail)
        module._run('owned','owned.zip','replace');self.assertEqual(job['status'],'failed');retained=job['error_detail']['recovered_as']
        self.assertIn(retained,backend);self.assertFalse(Path(job['error_detail']['sidecar_snapshots']).is_absolute());self.assertTrue((root/job['error_detail']['sidecar_snapshots']).is_dir())
        self.assertEqual(recovery.sweep(module.wc.get_client()),[]);self.assertIn(retained,backend)


class DeletedRecoveryTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from contextlib import ExitStack
        from unittest.mock import patch
        from types import SimpleNamespace
        from config import settings
        from services import collection_recovery as recovery,weaviate_client as wc,goldstandard as gs,ingest_config,retrieval_config
        self.recovery,self.wc=recovery,wc
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);self.root=Path(temp.name);stack=ExitStack();self.addCleanup(stack.close)
        for change in [patch.object(settings,'upload_dir',temp.name),patch.object(settings,'sources_dir',str(self.root/'sources')),patch.object(gs,'_sessions',{}),patch.object(ingest_config,'_DIR',None),patch.object(retrieval_config,'_DIR',None)]:stack.enter_context(change)
        self.backend={'OwnedRecovery'}
        class Collections:
            def exists(inner,name):return writes.canonical(name) in self.backend
            def delete(inner,name):self.backend.remove(writes.canonical(name))
            def get(inner,name):return SimpleNamespace(config=SimpleNamespace(get=lambda:SimpleNamespace(name=writes.canonical(name))),aggregate=SimpleNamespace(over_all=lambda **kw:SimpleNamespace(total_count=3)))
        self.client=SimpleNamespace(collections=Collections());stack.enter_context(patch.object(wc,'get_client',return_value=self.client))
        self.owner=recovery.begin('OwnedRecovery','tune',self.client);self.backend.add(self.owner['staging']);recovery.retain(self.owner)
    def test_explicit_alias_delete_retires_only_matching_recovery_snapshots(self):
        import json
        other=self.recovery.begin('OwnedRecovery','import',self.client);self.backend.add(other['staging']);self.recovery.retain(other)
        corrupt=self.recovery._root()/'invalid.json';corrupt.write_text(json.dumps({**self.owner,'operation_id':'invalid'}))
        name=self.owner['staging'];self.assertEqual(self.wc._delete_collection_sync(name[:1].lower()+name[1:]),3)
        self.assertNotIn(name,self.backend);self.assertFalse((self.recovery._root()/self.owner['operation_id']).exists());self.assertFalse((self.recovery._root()/(self.owner['operation_id']+'.json')).exists())
        self.assertIn(other['staging'],self.backend);self.assertTrue((self.recovery._root()/other['operation_id']).is_dir());self.assertTrue(corrupt.is_file())
    def test_caller_spelled_sidecars_and_sessions_are_cleaned_on_normal_delete(self):
        from services import sources,ingest_config,retrieval_config,goldstandard as gs
        caller='ownedRecovery';sources.store(caller,'inert.txt',b'Owned inert original')
        ingest_config.save({'collection':caller});retrieval_config.save({'collection':caller})
        session={'session_id':'gs_490abcde','collection':caller,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]};gs.store_session(session)
        self.wc._delete_collection_sync(caller)
        self.assertFalse(sources.collection_dir(caller).exists());self.assertFalse((self.root/'ingest_configs'/(caller+'.json')).exists());self.assertFalse((self.root/'retrieval_configs'/(caller+'.json')).exists())
        self.assertTrue(gs.get_session(session['session_id'])['orphaned']);self.assertIn(self.owner['staging'],self.backend)
    def test_alias_delete_cleans_both_spellings_and_preserves_distinct_collection(self):
        import json
        from unittest.mock import patch,call
        from services import sources,ingest_config,retrieval_config,goldstandard as gs
        canonical,caller,neighbor='OwnedRecovery','ownedRecovery','Ownedrecovery'
        self.backend.add(neighbor)
        identities={canonical:'gs_14000001',caller:'gs_14000002',neighbor:'gs_14000003'}
        for spelling,sid in identities.items():
            sources.store(spelling,'inert.txt',b'Owned original')
            ingest_config.save({'collection':spelling});retrieval_config.save({'collection':spelling})
            gs.store_session({'session_id':sid,'collection':spelling,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
        # Exact calls prove both paths even on a case-insensitive host volume.
        with patch.object(sources,'delete',wraps=sources.delete) as originals,patch.object(ingest_config,'delete',wraps=ingest_config.delete) as ingest,patch.object(retrieval_config,'delete',wraps=retrieval_config.delete) as retrieval:
            self.assertEqual(self.wc._delete_collection_sync(caller),3)
            for cleanup in (originals,ingest,retrieval):self.assertEqual(cleanup.call_args_list,[call(canonical),call(caller)])
        self.assertNotIn(canonical,self.backend);self.assertIn(neighbor,self.backend)
        for spelling in (canonical,caller):
            self.assertFalse(sources.collection_dir(spelling).exists());self.assertIsNone(ingest_config.load(spelling));self.assertIsNone(retrieval_config.load(spelling))
            session=gs.get_session(identities[spelling]);self.assertTrue(session['orphaned']);self.assertEqual(session['pairs'],[])
            self.assertTrue(json.loads(gs._session_path(identities[spelling]).read_text())['orphaned'])
        self.assertTrue(sources.collection_dir(neighbor).exists());self.assertIsNotNone(ingest_config.load(neighbor));self.assertIsNotNone(retrieval_config.load(neighbor));self.assertFalse(gs.get_session(identities[neighbor]).get('orphaned',False))
        self.assertIn(self.owner['staging'],self.backend);self.assertTrue((self.recovery._root()/self.owner['operation_id']).is_dir())
    def test_canonical_delete_cleans_once_and_orphans_canonical_session(self):
        from unittest.mock import patch,call
        from services import sources,ingest_config,retrieval_config,goldstandard as gs
        name='OwnedRecovery';sid='gs_14000004'
        sources.store(name,'owned.txt',b'Original');ingest_config.save({'collection':name});retrieval_config.save({'collection':name})
        gs.store_session({'session_id':sid,'collection':name,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
        with patch.object(sources,'delete',wraps=sources.delete) as cleanup:
            self.assertEqual(self.wc._delete_collection_sync(name),3);self.assertEqual(cleanup.call_args_list,[call(name),call('ownedRecovery')])
        self.assertTrue(gs.get_session(sid)['orphaned']);self.assertFalse(sources.collection_dir(name).exists());self.assertIsNone(ingest_config.load(name));self.assertIsNone(retrieval_config.load(name))
    def test_canonical_delete_cleans_historical_alias_sidecars_and_sessions(self):
        from unittest.mock import patch,call
        from services import sources,ingest_config,retrieval_config,goldstandard as gs
        canonical,alias,neighbor='OwnedRecovery','ownedRecovery','Ownedrecovery'
        self.backend.add(neighbor);identities={alias:'gs_14000006',neighbor:'gs_14000007'}
        for spelling,sid in identities.items():
            sources.store(spelling,'owned.txt',b'Owned historical original')
            ingest_config.save({'collection':spelling});retrieval_config.save({'collection':spelling})
            gs.store_session({'session_id':sid,'collection':spelling,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
        with patch.object(sources,'delete',wraps=sources.delete) as originals:
            self.assertEqual(self.wc._delete_collection_sync(canonical),3)
            self.assertEqual(originals.call_args_list,[call(canonical),call(alias)])
        self.assertFalse(sources.collection_dir(alias).exists())
        self.assertIsNone(ingest_config.load(alias));self.assertIsNone(retrieval_config.load(alias))
        self.assertTrue(gs.get_session(identities[alias]).get('orphaned',False))
        self.assertTrue(sources.collection_dir(neighbor).exists());self.assertIsNotNone(ingest_config.load(neighbor))
        self.assertFalse(gs.get_session(identities[neighbor]).get('orphaned',False))
    def test_failed_backend_delete_preserves_sidecars_and_current_session(self):
        from unittest.mock import patch
        from services import sources,ingest_config,retrieval_config,goldstandard as gs
        name='OwnedRecovery';sid='gs_14000005'
        sources.store(name,'owned.txt',b'Original');ingest_config.save({'collection':name});retrieval_config.save({'collection':name})
        gs.store_session({'session_id':sid,'collection':name,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
        with patch.object(self.client.collections,'delete',side_effect=OSError('Owned delete failure')):
            with self.assertRaisesRegex(OSError,'Owned delete failure'):self.wc._delete_collection_sync('ownedRecovery')
        self.assertTrue(sources.collection_dir(name).exists());self.assertIsNotNone(ingest_config.load(name));self.assertIsNotNone(retrieval_config.load(name));self.assertFalse(gs.get_session(sid).get('orphaned',False));self.assertIn(name,self.backend)
    def test_http_delete_removes_both_registry_spellings_only_after_success(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        from routers import collections as route
        for caller in ('OwnedRecovery', 'ownedRecovery'):
            for fails in (False, True):
                registry = {'OwnedRecovery': 'canonical', 'ownedRecovery': 'alias', 'Ownedrecovery': 'neighbor'}
                with self.subTest(caller=caller, fails=fails), \
                     patch.object(route.wc, 'collection_exists', new=AsyncMock(return_value=True)), \
                     patch.object(route.wc, 'delete_collection_when_idle', new=AsyncMock(side_effect=OSError('owned') if fails else None, return_value=3)), \
                     patch.object(route, '_registry_lock', asyncio.Lock()), \
                     patch.object(route, '_load_registry', return_value=registry), \
                     patch.object(route, '_save_registry') as save:
                    asyncio.run(route.delete_collection(caller, confirm='true'))
                    if fails:
                        save.assert_not_called()
                        self.assertEqual(len(registry), 3)
                    else:
                        save.assert_called_once_with({'Ownedrecovery': 'neighbor'})

    def test_alias_delete_orphan_reason_names_canonical_collection(self):
        # #140: whichever spelling the caller used, both sessions record the canonical name.
        import json
        from services import goldstandard as gs
        identities={'OwnedRecovery':'gs_14000008','ownedRecovery':'gs_14000009'}
        for spelling,sid in identities.items():
            gs.store_session({'session_id':sid,'collection':spelling,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
        self.wc._delete_collection_sync('ownedRecovery')
        for sid in identities.values():
            self.assertEqual(gs.get_session(sid).get('orphaned_reason'),"collection 'OwnedRecovery' was deleted",'in-memory orphan reason must name the canonical collection')
            self.assertEqual(json.loads(gs._session_path(sid).read_text()).get('orphaned_reason'),"collection 'OwnedRecovery' was deleted",'persisted orphan reason must name the canonical collection')
    def test_deleting_original_preserves_distinct_retained_recovery(self):
        self.wc._delete_collection_sync('OwnedRecovery');self.assertIn(self.owner['staging'],self.backend)
        self.assertTrue((self.recovery._root()/self.owner['operation_id']).is_dir())
    def test_missing_backend_at_startup_does_not_authorize_snapshot_loss(self):
        self.backend.remove(self.owner['staging']);self.assertEqual(self.recovery.sweep(self.client),[])
        self.assertTrue((self.recovery._root()/self.owner['operation_id']).is_dir())
    def test_explicit_cleanup_failure_is_resumed_from_durable_intent(self):
        import json
        from unittest.mock import patch
        with patch.object(self.recovery.shutil,'rmtree',side_effect=OSError('Owned cleanup failure')):
            with self.assertRaises(OSError):self.wc._delete_collection_sync(self.owner['staging'])
        journal=self.recovery._root()/(self.owner['operation_id']+'.json');self.assertEqual(json.loads(journal.read_text())['state'],'cleanup')
        self.assertEqual(self.recovery.sweep(self.client),[self.owner['staging']]);self.assertFalse(journal.exists());self.assertFalse((self.recovery._root()/self.owner['operation_id']).exists())



if __name__=='__main__':unittest.main()
