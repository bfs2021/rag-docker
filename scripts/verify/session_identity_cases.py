"""Owned import identity preservation; run by13_identity.sh in the API image."""
import asyncio,copy,json,os,subprocess,sys,tempfile,threading,time,unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR') or (str(Path(__file__).resolve().parents[2]/'api') if __file__!='<stdin>' else '/app'))
from config import settings
from services import goldstandard as gs,importer


def fixture():
    return {'session_id':'gs_460abcde','collection':'OwnedOriginal','status':'completed','pairs_total':1,'pairs_completed':1,'pairs':[{'pair_id':'p_owned','question':'Inert question','answer':'Original answer','contexts':['Inert context'],'ground_truth':'Inert truth','source_file':'inert.txt','chunk_index':0,'status':'approved'}]}


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        for obj,key,value in [(settings,'upload_dir',self.tmp.name),(settings,'sources_dir',str(Path(self.tmp.name)/'sources')),(gs,'_sessions',{})]:
            change=patch.object(obj,key,value);change.start();self.addCleanup(change.stop)
        self.original=fixture();gs.store_session(copy.deepcopy(self.original));self.path=gs._session_path(self.original['session_id'])

    def test_restore_twice_preserves_newer_original_review_and_independent_exports(self):
        package=Path(self.tmp.name)/'owned-package';gold=package/'goldstandard';gold.mkdir(parents=True)
        (gold/(self.original['session_id']+'.json')).write_text(json.dumps(self.original))
        asyncio.run(gs.update_pair(self.original['session_id'],'p_owned',{'answer':'Newer human review','status':'edited'}));before=self.path.read_bytes()
        imported=[]
        for target in ['OwnedRenamedOne','OwnedRenamedTwo']:
            mappings=[];validated=importer._read_goldstandard_sessions(package,'OwnedOriginal')
            notes=importer._restore_sidecars(target,package,'OwnedOriginal',validated,mappings)
            self.assertEqual(len(mappings),1);self.assertTrue(any(mappings[0]['session_id'] in note for note in notes))
            session=gs.get_session(mappings[0]['session_id']);imported.append(session)
            self.assertEqual(session['imported_from']['session_id'],self.original['session_id']);self.assertEqual(session['imported_from']['collection'],'OwnedOriginal')
        identities=[self.original['session_id']]+[s['session_id'] for s in imported];self.assertEqual(len(set(identities)),3);self.assertEqual(self.path.read_bytes(),before)
        for i,sid in enumerate(identities):
            result=asyncio.run(gs.save_session(sid,'owned-export'+str(i)+'.json'));rows=json.loads((Path(self.tmp.name)/gs.EXPORT_DIRNAME/result['filename']).read_text())
            self.assertEqual(rows[0]['answer'],'Newer human review' if i==0 else 'Original answer');self.assertEqual(set(rows[0]),{'question','answer','contexts','ground_truth'})
        gs._sessions={};gs.load_sessions_from_disk();self.assertEqual({gs.get_session(sid)['collection'] for sid in identities},{'OwnedOriginal','OwnedRenamedOne','OwnedRenamedTwo'})

    def test_concurrent_imports_select_distinct_local_identities(self):
        before=self.path.read_bytes()
        def run(i):
            data=fixture();data['collection']='OwnedImported'+str(i)
            return gs.store_imported_session(data,'OwnedOriginal')['session_id']
        with ThreadPoolExecutor(max_workers=8) as pool:identities=list(pool.map(run,range(16)))
        self.assertEqual(len(set(identities)),16);self.assertNotIn(self.original['session_id'],identities);self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(list(self.path.parent.glob('*.json'))),17)

    def test_cold_cache_still_respects_existing_disk_identity(self):
        before=self.path.read_bytes();gs._sessions={}
        result=gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertNotEqual(result['session_id'],self.original['session_id']);self.assertEqual(self.path.read_bytes(),before)

    def test_unreadable_original_bytes_still_occupy_the_identity(self):
        self.path.write_bytes(b'{owned retained unreadable bytes');gs._sessions={}
        result=gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertNotEqual(result['session_id'],self.original['session_id']);self.assertEqual(self.path.read_bytes(),b'{owned retained unreadable bytes')

    def test_collision_exhaustion_is_bounded_without_overwrite(self):
        before=self.path.read_bytes()
        with patch.object(gs.uuid,'uuid4',return_value=SimpleNamespace(hex='460abcde'+'0'*24)) as ids:
            with self.assertRaisesRegex(RuntimeError,'unoccupied session'):gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertEqual(ids.call_count,128);self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(gs._sessions),1)

    def test_redirected_existing_identity_is_reserved_without_following_it(self):
        foreign=Path(self.tmp.name)/'owned-retained-neighbor';foreign.write_bytes(b'owned retained bytes')
        self.path.unlink();self.path.symlink_to(foreign);gs._sessions={}
        saved=gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertNotEqual(saved['session_id'],self.original['session_id']);self.assertTrue(self.path.is_symlink())
        self.assertEqual(foreign.read_bytes(),b'owned retained bytes')

    def test_cache_readers_skip_symlinks_and_fifo_without_opening_them(self):
        directory=gs._sessions_dir();os.mkfifo(directory/'gs_460aa001.json')
        foreign=Path(self.tmp.name)/'owned-foreign.json';data=fixture();data['pairs'][0]['answer']='Foreign bytes'
        foreign.write_text(json.dumps(data));(directory/'gs_460aa002.json').symlink_to(foreign)
        code="from config import settings;from services import goldstandard as gs;import sys;settings.upload_dir=sys.argv[1];gs.load_sessions_from_disk();assert len(gs.sessions_for('OwnedOriginal'))==1;assert gs.get_session('gs_460abcde')['pairs'][0]['answer']=='Original answer'"
        subprocess.run([sys.executable,'-c',code,self.tmp.name],env={**os.environ,'PYTHONPATH':str(Path(gs.__file__).parents[1])},check=True,timeout=5,capture_output=True)
        self.assertTrue((directory/'gs_460aa001.json').exists());self.assertTrue((directory/'gs_460aa002.json').is_symlink());self.assertEqual(json.loads(foreign.read_text())['pairs'][0]['answer'],'Foreign bytes')

    def test_free_valid_source_identity_is_retained_with_provenance(self):
        data=fixture();data['session_id']='gs_460abcdf';data['collection']='OwnedNew'
        result=gs.store_imported_session(data,'ExternalOriginal')
        self.assertEqual(result['session_id'],data['session_id']);self.assertEqual(result['imported_from']['collection'],'ExternalOriginal')
        self.assertTrue(result['imported_from']['imported_at'].endswith('+00:00'))

    def test_inputs_and_returned_snapshots_do_not_alias_imported_storage(self):
        data=fixture();before=copy.deepcopy(data);result=gs.store_imported_session(data,'OwnedOriginal')
        result['pairs'][0]['answer']='Returned mutation';data['pairs'][0]['answer']='Caller mutation'
        self.assertEqual(gs.get_session(result['session_id'])['pairs'][0]['answer'],'Original answer');self.assertNotIn('imported_from',before)

    def test_storage_inspection_failure_never_guesses_a_free_slot(self):
        before=self.path.read_bytes()
        with patch.object(Path,'lstat',side_effect=PermissionError('Owned identity inspection failure')):
            with self.assertRaises(PermissionError):gs.store_imported_session(fixture(),'OwnedOriginal')
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(gs._sessions),1)

    def test_generation_start_uses_the_same_namespace_without_overwriting_collision(self):
        before=self.path.read_bytes()
        async def run():
            with patch.object(gs.wc,'_sample_chunks_sync',new=MagicMock(return_value=[])),patch.object(gs,'_run_generation',new=AsyncMock()),patch.object(gs.uuid,'uuid4',side_effect=[SimpleNamespace(hex='460abcde'+'0'*24),SimpleNamespace(hex='460abcdf'+'0'*24)]):
                result=await gs.start_generation('OwnedGeneration',1,None)
                await asyncio.gather(*list(gs._tasks))
            self.assertEqual(result['session_id'],'gs_460abcdf')
        asyncio.run(run());self.assertEqual(self.path.read_bytes(),before)

    def test_concurrent_generated_and_imported_sessions_share_identity_serialization(self):
        before=self.path.read_bytes()
        def run(i):
            data=fixture();data['collection']='OwnedCreated'+str(i)
            saved=(gs.store_imported_session(data,'OwnedOriginal') if i%2 else gs._store_generated_session(data))
            return saved['session_id']
        with ThreadPoolExecutor(max_workers=8) as pool:identities=list(pool.map(run,range(16)))
        self.assertEqual(len(set(identities)),16);self.assertNotIn(self.original['session_id'],identities);self.assertEqual(self.path.read_bytes(),before)

    def test_noncanonical_source_identity_is_refused_before_restoration(self):
        package=Path(self.tmp.name)/'legacy-package';gold=package/'goldstandard';gold.mkdir(parents=True)
        data=fixture();data['session_id']='legacy-review-2024';(gold/'legacy.json').write_text(json.dumps(data))
        before=self.path.read_bytes()
        with self.assertRaises(importer.PackageError) as caught:importer._read_goldstandard_sessions(package,'OwnedOriginal')
        self.assertEqual((caught.exception.code,caught.exception.detail),('PACKAGE_CORRUPT',{'file':'goldstandard/legacy.json'}))
        self.assertEqual(list(gs._sessions),[self.original['session_id']]);self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(len(list(self.path.parent.glob('*.json'))),1)

    def test_cache_iteration_serializes_with_generation_insertion(self):
        entered=threading.Event();release=threading.Event();started=threading.Event();mutated=threading.Event()
        class PausedCache(dict):
            def __setitem__(cache,key,value):
                super(PausedCache,cache).__setitem__(key,value);mutated.set()
            def items(cache):
                iterator=iter(super(PausedCache,cache).items())
                entered.set();self.assertTrue(release.wait(2),'Cache fixture was not released')
                return iterator
        gs._sessions=PausedCache(gs._sessions)
        with ThreadPoolExecutor(max_workers=2) as pool:
            reading=pool.submit(gs.sessions_for,'OwnedOriginal');self.assertTrue(entered.wait(2))
            def create():
                started.set();return gs._store_generated_session(fixture())
            writing=pool.submit(create);self.assertTrue(started.wait(2))
            try:self.assertFalse(mutated.wait(.1),'Insertion bypassed the cache snapshot lock')
            finally:release.set()
            self.assertEqual(len(reading.result(timeout=2)),1);self.assertRegex(writing.result(timeout=2)['session_id'],r'^gs_[0-9a-f]{8}$')


    # Reviewer-added: forced candidate collisions under real thread contention.
    def test_reviewer_forced_duplicate_candidates_race_never_share_or_overwrite(self):
        before=self.path.read_bytes();shared='gs_460abc01';draw=threading.Lock();counter=[0]
        def duplicated():
            with draw:
                n=counter[0];counter[0]+=1
            return SimpleNamespace(hex='%08x'%(0x46100000+n//2)+'0'*24)
        original_save=gs._save_session_sync
        def slow_save(session):
            time.sleep(.02);return original_save(session)
        def run(i):
            data=fixture();data['collection']='OwnedRace'+str(i)
            if i%3==0:return gs._store_generated_session(data)['session_id']
            data['session_id']=shared if i%3==1 else self.original['session_id']
            return gs.store_imported_session(data,'OwnedOriginal')['session_id']
        with patch.object(gs.uuid,'uuid4',side_effect=duplicated),patch.object(gs,'_save_session_sync',side_effect=slow_save):
            with ThreadPoolExecutor(max_workers=12) as pool:identities=list(pool.map(run,range(24)))
        self.assertEqual(len(set(identities)),24,identities);self.assertNotIn(self.original['session_id'],identities)
        self.assertEqual(identities.count(shared),1,'exactly one concurrent import keeps a free shared source ID')
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(list(self.path.parent.glob('*.json'))),25)
        for sid in identities:self.assertEqual(json.loads((self.path.parent/(sid+'.json')).read_text())['session_id'],sid)

    def test_reviewer_replace_import_into_same_collection_keeps_newer_original(self):
        package=Path(self.tmp.name)/'owned-replace-package';gold=package/'goldstandard';gold.mkdir(parents=True)
        (gold/(self.original['session_id']+'.json')).write_text(json.dumps(self.original))
        asyncio.run(gs.update_pair(self.original['session_id'],'p_owned',{'answer':'Newer human review','status':'edited'}));before=self.path.read_bytes()
        mappings=[];validated=importer._read_goldstandard_sessions(package,'OwnedOriginal')
        importer._restore_sidecars('OwnedOriginal',package,'OwnedOriginal',validated,mappings)
        self.assertNotEqual(mappings[0]['session_id'],self.original['session_id']);self.assertEqual(self.path.read_bytes(),before)
        self.assertEqual(gs.get_session(self.original['session_id'])['pairs'][0]['answer'],'Newer human review')

    def test_reviewer_generation_identity_inspection_failure_is_session_write_failed_503(self):
        before=self.path.read_bytes()
        async def run():
            with patch.object(gs.wc,'_sample_chunks_sync',new=MagicMock(return_value=[])),patch.object(gs,'_run_generation',new=AsyncMock()) as generate,patch.object(Path,'lstat',side_effect=PermissionError('Owned identity inspection failure')):
                with self.assertRaises(gs.GoldStandardError) as caught:await gs.start_generation('OwnedGeneration',1,None)
                generate.assert_not_called()
            return caught.exception
        error=asyncio.run(run())
        self.assertEqual((error.code,error.status),('SESSION_WRITE_FAILED',503));self.assertEqual(self.path.read_bytes(),before)

    def test_generation_allocation_failure_records_diagnostic_for_candidate(self):
        storage=Path(settings.upload_dir)/'goldstandard_sessions';before=self.path.read_bytes()
        for name,changes,expected in [('inspection',[patch.object(gs.uuid,'uuid4',return_value=SimpleNamespace(hex='460abcdf'+'0'*24)),patch.object(Path,'lstat',side_effect=PermissionError('Owned identity inspection failure'))],'gs_460abcdf.json'),
                                      ('exhaustion',[patch.object(gs.uuid,'uuid4',return_value=SimpleNamespace(hex='460abcde'+'0'*24))],'gs_460abcde.json')]:
            with self.subTest(case=name),patch.object(gs,'_diagnostics',{}):
                with ExitStack() as stack:
                    for change in changes:stack.enter_context(change)
                    with self.assertRaises(gs.GoldStandardError) as caught:gs._store_generated_session(fixture())
                self.assertEqual((caught.exception.code,caught.exception.status),('SESSION_WRITE_FAILED',503))
                self.assertEqual({key:issue['code'] for key,issue in gs._diagnostics.items()},{str(storage/expected):'SESSION_WRITE_FAILED'})
                self.assertEqual(list(gs._sessions),[self.original['session_id']]);self.assertEqual(self.path.read_bytes(),before)

    # Testing reviewer (#148): the ValueError branch, the HTTP envelope and grammar boundaries.
    def test_reviewer_generation_redirected_storage_root_is_session_write_failed_503(self):
        storage=Path(settings.upload_dir)/'goldstandard_sessions';moved=Path(self.tmp.name)/'owned-moved-sessions'
        storage.rename(moved);storage.symlink_to(moved);before=sorted(p.name for p in moved.iterdir())
        with patch.object(gs,'_diagnostics',{}),patch.object(gs.uuid,'uuid4',return_value=SimpleNamespace(hex='460abc11'+'0'*24)):
            with self.assertRaises(gs.GoldStandardError) as caught:gs._store_generated_session(fixture())
            self.assertEqual((caught.exception.code,caught.exception.status),('SESSION_WRITE_FAILED',503))
            self.assertIsInstance(caught.exception.__cause__,ValueError)
            self.assertEqual({key:issue['code'] for key,issue in gs._diagnostics.items()},{str(storage/'gs_460abc11.json'):'SESSION_WRITE_FAILED'})
        self.assertEqual(sorted(p.name for p in moved.iterdir()),before);self.assertEqual(list(gs._sessions),[self.original['session_id']])

    def test_reviewer_generate_route_returns_503_session_write_failed_envelope(self):
        from routers import goldstandard as route
        from models.schemas import GenerateRequest
        before=self.path.read_bytes()
        async def run():
            with patch.object(route.wc,'collection_exists',new=AsyncMock(return_value=True)),patch.object(gs.wc,'_sample_chunks_sync',new=MagicMock(return_value=[])),patch.object(gs,'_run_generation',new=AsyncMock()) as generate,patch.object(Path,'lstat',side_effect=PermissionError('Owned identity inspection failure')):
                response=await route.generate(GenerateRequest(collection='OwnedGeneration',sample_size=1,seed=None))
                generate.assert_not_called()
            return response
        response=asyncio.run(run())
        self.assertEqual(response.status_code,503);self.assertEqual(json.loads(response.body)['error']['code'],'SESSION_WRITE_FAILED')
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(list(gs._sessions),[self.original['session_id']])

    def test_reviewer_preflight_refuses_near_canonical_and_unbounded_source_ids(self):
        before=self.path.read_bytes()
        for source in ['gs_460ABCDE','GS_460abcde','gs_460abcd','gs_460abcde0','gs_460abcdg',' gs_460abcde','gs_460abcde\n','gs_'+'a'*10000,'',460]:
            with self.subTest(source=repr(source)[:40]):
                package=Path(tempfile.mkdtemp(dir=self.tmp.name));gold=package/'goldstandard';gold.mkdir()
                data=fixture();data['session_id']=source;(gold/'owned.json').write_text(json.dumps(data))
                with self.assertRaises(importer.PackageError) as caught:importer._read_goldstandard_sessions(package,'OwnedOriginal')
                self.assertEqual((caught.exception.code,caught.exception.detail),('PACKAGE_CORRUPT',{'file':'goldstandard/owned.json'}))
        package=Path(tempfile.mkdtemp(dir=self.tmp.name));gold=package/'goldstandard';gold.mkdir()
        data=fixture();data['session_id']='gs_0123abcd';(gold/'owned.json').write_text(json.dumps(data))
        self.assertEqual([s['session_id'] for s in importer._read_goldstandard_sessions(package,'OwnedOriginal')],['gs_0123abcd'])
        self.assertEqual(self.path.read_bytes(),before);self.assertEqual(len(list(self.path.parent.glob('*.json'))),1)

if __name__=='__main__':unittest.main()
