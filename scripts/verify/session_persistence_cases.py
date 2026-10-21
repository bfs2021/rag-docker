"""Durable acknowledged mutations and controlled failure/interleaving acceptance."""
import asyncio,json,os,sys,tempfile,threading,unittest,subprocess,time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock,MagicMock,patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR') or (str(Path(__file__).resolve().parents[2]/'api') if __file__ != '<stdin>' else '/app'))
import httpx
from config import settings
from main import app
from services import goldstandard as gs


def fixture():
    return {'session_id':'gs_450abcde','collection':'OwnedPersistence','status':'completed','pairs_total':2,'pairs_completed':2,'pairs':[{'pair_id':'p_'+str(i),'question':'Original','answer':'Original','contexts':['Inert'],'ground_truth':'Original','source_file':'inert.txt','chunk_index':i,'status':'pending'} for i in range(2)]}

def child_env():
    return {**os.environ,'PYTHONPATH':os.environ.get('RAG_TEST_API_DIR') or (str(Path(__file__).resolve().parents[2]/'api') if __file__ != '<stdin>' else '/app')}

def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app,raise_app_exceptions=False),base_url='http://owned-review')

class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        patches=[(settings,'upload_dir',self.tmp.name),(settings,'sources_dir',str(Path(self.tmp.name)/'sources')),(gs,'_sessions',{})]
        if hasattr(gs,'_diagnostics'):patches.append((gs,'_diagnostics',{}))
        if hasattr(gs,'_scan_cache'):patches.append((gs,'_scan_cache',{}))
        for obj,key,value in patches:
            change=patch.object(obj,key,value);change.start();self.addCleanup(change.stop)
        self.data=fixture();gs.store_session(self.data)

    def restart(self):
        gs._sessions={};gs.load_sessions_from_disk();return gs.get_session(self.data['session_id'])

    def test_startup_settles_interrupted_generation_durably_in_fresh_process(self):
        self.data.update(status='generating', pairs_total=5, pairs_attempted=2, stale=True, stale_reason='Old corpus')
        gs.store_session(self.data)
        script = """
import asyncio, json, sys
from unittest.mock import patch, AsyncMock
from config import settings
settings.upload_dir = sys.argv[1]
from main import app, lifespan
from services import goldstandard as gs, weaviate_client as wc, importer, packager
async def run():
    with patch.object(wc, 'sweep_staging', new=AsyncMock(return_value=[])), patch.object(importer, 'sweep_interrupted_imports', return_value=[]), patch.object(importer, 'sweep_stale_workdirs', return_value=[]), patch.object(packager, 'sweep_build_leftovers', return_value=[]):
        async with lifespan(app):
            print(json.dumps(gs.get_session('gs_450abcde')))
asyncio.run(run())
"""
        first = json.loads(subprocess.check_output([sys.executable, '-c', script, self.tmp.name], env=child_env(), text=True))
        second = json.loads(subprocess.check_output([sys.executable, '-c', script, self.tmp.name], env=child_env(), text=True))
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'failed')
        for field in ('pairs', 'pairs_total', 'pairs_attempted', 'pairs_completed', 'stale', 'stale_reason'):
            self.assertEqual(first[field], self.data[field])
        self.assertTrue(any('interrupted by API restart' in error for error in first['errors']))
        self.assertEqual(json.loads(gs._session_path(self.data['session_id']).read_text()), first)

    def test_startup_write_failure_is_visible_and_ordinary_scans_leave_workers_alone(self):
        self.data['status'] = 'generating'
        gs.store_session(self.data)
        gs.session_diagnostics()
        self.assertEqual(gs.get_session(self.data['session_id'])['status'], 'generating')
        before = gs._session_path(self.data['session_id']).read_bytes()
        with patch.object(gs.os, 'replace', side_effect=OSError('Owned startup write fault')):
            gs.reconcile_interrupted_generations()
        current = gs.get_session(self.data['session_id'])
        self.assertEqual(current['status'], 'failed')
        self.assertEqual(current['persistence_error']['code'], 'SESSION_WRITE_FAILED')
        self.assertEqual(current['pairs'], self.data['pairs'])
        self.assertEqual(gs._session_path(self.data['session_id']).read_bytes(), before)
        gs._sessions = {}
        gs.load_sessions_from_disk()
        gs.reconcile_interrupted_generations()
        on_disk = json.loads(gs._session_path(self.data['session_id']).read_text())
        self.assertEqual(on_disk['status'], 'failed')
        self.assertTrue(any('interrupted by API restart' in e for e in on_disk['errors']))
        self.assertEqual(gs.get_session(self.data['session_id']), on_disk)

    def test_startup_settles_only_interrupted_session_keeping_edits_and_flags(self):
        done_before = gs._session_path(self.data['session_id']).read_bytes()
        live = fixture(); live['session_id'] = 'gs_450fedcb'
        live['pairs'][0].update(status='edited', question='Reviewer edit')
        live.update(status='generating', pairs_total=4, pairs_attempted=2,
                    pairs_failed=0, stale=True, stale_reason='Old corpus',
                    stale_at='2026-01-01T00:00:00+00:00', orphaned=True,
                    orphaned_reason='Collection deleted', orphaned_at='2026-01-02T00:00:00+00:00')
        gs.store_session(live)
        gs._sessions = {}; gs.load_sessions_from_disk()
        gs.reconcile_interrupted_generations()
        self.assertEqual(gs._session_path(self.data['session_id']).read_bytes(), done_before)
        settled = json.loads(gs._session_path(live['session_id']).read_text())
        self.assertEqual(settled['status'], 'failed')
        for field in ('pairs', 'pairs_total', 'pairs_attempted', 'pairs_completed',
                      'pairs_failed', 'stale', 'stale_reason', 'stale_at',
                      'orphaned', 'orphaned_reason', 'orphaned_at'):
            self.assertEqual(settled[field], live[field], field)
        self.assertEqual(settled['errors'], ['Generation interrupted by API restart. Retained pairs are available for review; start a new generation for missing pairs.'])
        self.assertNotIn('persistence_error', settled)
        gs._sessions = {}; gs.load_sessions_from_disk()
        self.assertEqual(gs.get_session(live['session_id']), settled)
        new_pair = {**live['pairs'][1], 'question': 'Regenerated'}
        with patch.object(gs, '_generate_pair', new=AsyncMock(return_value=new_pair)):
            result = asyncio.run(gs.regenerate_pair(live['session_id'], live['pairs'][1]['pair_id']))
        self.assertEqual(result['question'], 'Regenerated')
        stored = json.loads(gs._session_path(live['session_id']).read_text())
        self.assertEqual(stored['pairs'], [live['pairs'][0], new_pair])

    def test_reconcile_refuses_to_run_while_generation_workers_exist(self):
        self.data['status'] = 'generating'; gs.store_session(self.data)
        before = gs._session_path(self.data['session_id']).read_bytes()
        with patch.object(gs, '_tasks', {object()}):
            with self.assertRaisesRegex(RuntimeError, 'before workers start'):
                gs.reconcile_interrupted_generations()
        self.assertEqual(gs.get_session(self.data['session_id']), self.data)
        self.assertEqual(gs._session_path(self.data['session_id']).read_bytes(), before)

    def test_generation_accounts_for_partial_and_total_model_failure(self):
        for succeeds in (True, False):
            with self.subTest(succeeds=succeeds):
                data=fixture(); data.update(status='generating', pairs=[], pairs_total=2,
                                           pairs_completed=0, pairs_attempted=0, pairs_failed=0, errors=[])
                gs.store_session(data)
                effects=[fixture()['pairs'][0] if succeeds else ValueError('Invalid first pair'),
                         httpx.ReadTimeout('')]
                with patch.object(gs, '_generate_pair', new=AsyncMock(side_effect=effects)):
                    asyncio.run(gs._run_generation(data['session_id'], [{'content':'a'}, {'content':'b'}]))
                result=json.loads(gs._session_path(data['session_id']).read_text())
                self.assertEqual(result['status'], 'completed' if succeeds else 'failed')
                self.assertEqual(result['pairs_completed'], len(result['pairs']))
                self.assertEqual(result['pairs_completed'], int(succeeds))
                self.assertEqual(result['pairs_completed']+result['pairs_failed'], 2)
                self.assertEqual(result['pairs_attempted'], 2)
                self.assertEqual(len(result['errors']), result['pairs_failed'])
                self.assertTrue(all(reason.strip() for reason in result['errors']))

    def test_regeneration_deadline_returns_typed_error_and_preserves_pair(self):
        before=gs._session_path(self.data['session_id']).read_bytes()
        cancelled=[]
        async def delayed(chunk):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        async def run():
            with patch.object(gs, '_generate_pair', side_effect=delayed), patch.object(gs, '_REGENERATION_TIMEOUT_SECONDS', 0.01):
                async with client() as c:
                    return await c.post('/goldstandard/regenerate', json={'session_id':self.data['session_id'], 'pair_id':'p_0'})
        response=asyncio.run(run())
        self.assertEqual(response.status_code, 504, response.text)
        self.assertEqual(response.json()['error']['code'], 'PAIR_GENERATION_TIMEOUT')
        self.assertEqual(cancelled, [True])
        self.assertEqual(gs.get_session(self.data['session_id']), self.data)
        self.assertEqual(gs._session_path(self.data['session_id']).read_bytes(), before)

    def test_regeneration_retains_validation_and_transport_retry_budget(self):
        valid=json.dumps({'question':'Regenerated', 'answer':'Answer', 'ground_truth':'Truth'})
        replies=[httpx.ReadTimeout(''), 'invalid', httpx.ReadTimeout(''), 'invalid', httpx.ReadTimeout(''), valid]
        with patch.object(gs.ollama, 'chat', new=AsyncMock(side_effect=replies)) as chat:
            result=asyncio.run(gs.regenerate_pair(self.data['session_id'], 'p_0'))
        self.assertEqual(chat.await_count, 6)
        self.assertEqual(result['question'], 'Regenerated')
        self.assertEqual(result['pair_id'], 'p_0')
        stored=json.loads(gs._session_path(self.data['session_id']).read_text())
        self.assertEqual(stored['pairs'][1], self.data['pairs'][1])
        self.assertEqual(stored['pairs'][0], result)

    def test_startup_leaves_completed_sessions_unchanged(self):
        before = gs._session_path(self.data['session_id']).read_bytes()
        gs.reconcile_interrupted_generations()
        self.assertEqual(gs._session_path(self.data['session_id']).read_bytes(), before)

    def test_parallel_acknowledged_fields_survive_restart(self):
        data=fixture();data["pairs"]=[{**data["pairs"][0],"pair_id":"p_"+str(i)} for i in range(8)];data.update(pairs_total=8,pairs_completed=8);gs.store_session(data)
        barrier=threading.Barrier(16)
        def edit(i):
            barrier.wait();return asyncio.run(gs.update_pair(self.data['session_id'],'p_'+str(i//2),{('question' if i%2==0 else 'answer'):str(i)}))
        with ThreadPoolExecutor(max_workers=16) as pool:results=list(pool.map(edit,range(16)))
        self.assertTrue(all(results));state=self.restart()
        for i in range(16):self.assertEqual(state['pairs'][i//2]['question' if i%2==0 else 'answer'],str(i))

    def test_slow_first_write_cannot_replace_later_acknowledged_edit(self):
        path=gs._session_path(self.data['session_id'])
        entered=threading.Event();calls=0
        original_write=Path.write_text;original_replace=os.replace
        def pause_first_write():
            nonlocal calls
            calls+=1
            if calls==1:
                entered.set();time.sleep(0.8)
        def write_text(target,*args,**kwargs):
            if Path(target)==path:pause_first_write()
            return original_write(target,*args,**kwargs)
        def replace(src,dst):
            if Path(dst)==path:pause_first_write()
            return original_replace(src,dst)
        async def run():
            with patch.object(Path,'write_text',write_text),patch.object(os,'replace',side_effect=replace):
                first=asyncio.create_task(gs.update_pair(self.data['session_id'],'p_0',{'question':'First acknowledged'}))
                self.assertTrue(await asyncio.to_thread(entered.wait,5))
                second=asyncio.create_task(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Second acknowledged'}))
                return await asyncio.gather(first,second)
        results=asyncio.run(run())
        self.assertEqual(len(results),2)
        pair=self.restart()['pairs'][0]
        self.assertEqual((pair['question'],pair['answer']),('First acknowledged','Second acknowledged'))

    def test_replace_failure_leaves_previous_snapshot_and_reports_failure(self):
        path=gs._session_path(self.data['session_id']);before=path.read_bytes()
        with patch.object(gs.os,'replace',side_effect=OSError('Controlled replace fault')):
            with self.assertRaises(gs.GoldStandardError) as error:asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Failed edit'}))
        self.assertEqual(error.exception.code,'SESSION_WRITE_FAILED')
        self.assertEqual(path.read_bytes(),before);self.assertEqual(gs.get_session(self.data['session_id']),self.data)
        self.assertFalse(list(path.parent.glob('*.tmp')))
        self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_WRITE_FAILED')
        asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Accepted edit'}));self.assertEqual(gs.session_diagnostics(),[])

    def test_file_fsync_failure_leaves_old_snapshot(self):
        path=gs._session_path(self.data['session_id']);before=path.read_bytes()
        with patch.object(gs.os,'fsync',side_effect=OSError('Controlled file fsync')):
            with self.assertRaises(gs.GoldStandardError):asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Not accepted'}))
        self.assertEqual(path.read_bytes(),before);self.assertEqual(gs.get_session(self.data['session_id']),self.data)

    def test_after_replace_failure_is_uncertain_and_cache_matches_disk(self):
        with patch.object(gs,'_sync_directory',side_effect=OSError('Controlled directory fsync')):
            with self.assertRaises(gs.GoldStandardError) as error:asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Replaced'}))
        self.assertEqual(error.exception.code,'SESSION_DURABILITY_UNCERTAIN')
        self.assertEqual(json.loads(gs._session_path(self.data['session_id']).read_text()),gs.get_session(self.data['session_id']))
        self.assertEqual(gs.get_session(self.data['session_id'])['pairs'][0]['answer'],'Replaced')

    def test_unreadable_and_invalid_files_are_preserved_reported(self):
        files={'gs_450bad00.json':'{incomplete','gs_450bad01.json':'[]','gs_450bad02.json':'{"session_id":"gs_450bad02","pairs":[]}'}
        for name,text in files.items():(gs._sessions_dir()/name).write_text(text)
        self.assertEqual(len(gs.session_diagnostics()),3)
        for name,text in files.items():self.assertEqual((gs._sessions_dir()/name).read_text(),text)
        self.assertEqual(len(gs.sessions_for('OwnedPersistence')),1)

    def test_no_mutable_aliases_from_storage_reads_or_exports(self):
        self.data['pairs'][0]['answer']='Caller mutation'
        gs.get_session(self.data['session_id'])['pairs'][0]['answer']='Reader mutation'
        gs.sessions_for('OwnedPersistence')[0]['pairs'].clear()
        self.assertEqual(self.restart()['pairs'][0]['answer'],'Original')

    def test_generation_review_flags_interleave_without_lost_updates(self):
        async def run():
            pending=asyncio.Event();release=asyncio.Event()
            initial=fixture();initial.update(status='generating',pairs_total=3);gs.store_session(initial)
            async def pair(chunk):pending.set();await release.wait();return {**initial['pairs'][0],'pair_id':'p_generated'}
            with patch.object(gs,'_generate_pair',side_effect=pair):
                task=asyncio.create_task(gs._run_generation(initial['session_id'],[{'content':'Inert'}]));await pending.wait()
                await asyncio.gather(gs.update_pair(initial['session_id'],'p_0',{'answer':'Reviewed answer'}),gs.update_pair(initial['session_id'],'p_1',{'question':'Reviewed question'}))
                await asyncio.to_thread(gs.mark_stale,'OwnedPersistence','Controlled identity change')
                release.set();await task
        asyncio.run(run());state=self.restart();self.assertEqual(state['status'],'completed');self.assertEqual(len(state['pairs']),3)
        self.assertEqual(state['pairs'][0]['answer'],'Reviewed answer');self.assertEqual(state['pairs'][1]['question'],'Reviewed question');self.assertTrue(state['stale']);self.assertEqual(state['pairs_attempted'],1)

    def test_regeneration_rejects_changed_target_preserving_acknowledged_edit(self):
        async def run():
            pending=asyncio.Event();release=asyncio.Event()
            async def pair(chunk):pending.set();await release.wait();return {**fixture()['pairs'][0],'answer':'Generated replacement'}
            with patch.object(gs,'_generate_pair',side_effect=pair):
                task=asyncio.create_task(gs.regenerate_pair(self.data['session_id'],'p_0'));await pending.wait()
                await gs.update_pair(self.data['session_id'],'p_0',{'answer':'Acknowledged review'});release.set()
                with self.assertRaises(gs.GoldStandardError) as error:await task
                self.assertEqual(error.exception.code,'PAIR_CHANGED_DURING_REGENERATION')
        asyncio.run(run());self.assertEqual(self.restart()['pairs'][0]['answer'],'Acknowledged review')

    def test_regeneration_preserves_other_pair_edits_and_history_flags(self):
        async def run():
            pending=asyncio.Event();release=asyncio.Event()
            async def pair(chunk):pending.set();await release.wait();return {**fixture()['pairs'][0],'answer':'Generated replacement'}
            with patch.object(gs,'_generate_pair',side_effect=pair):
                task=asyncio.create_task(gs.regenerate_pair(self.data['session_id'],'p_0'));await pending.wait()
                await gs.update_pair(self.data['session_id'],'p_1',{'answer':'Other review'})
                gs.mark_orphaned('OwnedPersistence','Controlled deletion');release.set();await task
        asyncio.run(run());state=self.restart();self.assertTrue(state['orphaned']);self.assertEqual([p['answer'] for p in state['pairs']],['Generated replacement','Other review'])

    def test_hard_killed_writer_preserves_valid_snapshot_reports_owned_temporary(self):
        path=gs._session_path(self.data['session_id']);before=path.read_bytes();marker=Path(self.tmp.name)/'replace-boundary'
        code="""import sys,time,asyncio
from pathlib import Path
from config import settings
from services import goldstandard as gs
settings.upload_dir=sys.argv[1]
gs.load_sessions_from_disk()
def stopped(src,dst):
    Path(sys.argv[2]).write_text('ready')
    time.sleep(30)
gs.os.replace=stopped
asyncio.run(gs.update_pair('gs_450abcde','p_0',{'answer':'Interrupted edit'}))
"""
        env={**os.environ,'PYTHONPATH':os.environ.get('RAG_TEST_API_DIR') or (str(Path(__file__).resolve().parents[2]/'api') if __file__ != '<stdin>' else '/app')}
        child=subprocess.Popen([sys.executable,'-c',code,self.tmp.name,str(marker)],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        try:
            deadline=time.monotonic()+5
            while not marker.exists() and child.poll() is None and time.monotonic()<deadline:time.sleep(0.02)
            self.assertTrue(marker.exists(),'Owned child did not reach replace boundary')
            child.kill();child.communicate(timeout=5)
            self.assertEqual(path.read_bytes(),before);self.assertEqual(self.restart(),self.data)
            issues=gs.session_diagnostics();self.assertEqual(len(issues),1);self.assertEqual(issues[0]['code'],'SESSION_INTERRUPTED_WRITE')
            self.assertTrue(list(path.parent.glob('.gs_450abcde-*.tmp')))
        finally:
            if child.poll() is None:child.kill();child.communicate(timeout=5)

    def test_export_keeps_captured_snapshot_while_new_edit_commits(self):
        initial=fixture();initial['pairs'][0]['status']='approved';gs.store_session(initial)
        started=threading.Event();release=threading.Event();original=gs._save_export_sync
        def delayed(path,rows):
            started.set()
            if not release.wait(5):raise AssertionError('Owned export was not released')
            original(path,rows)
        async def run():
            with patch.object(gs,'_save_export_sync',side_effect=delayed):
                task=asyncio.create_task(gs.save_session(initial['session_id'],'snapshot.json'))
                self.assertTrue(await asyncio.to_thread(started.wait,5))
                try:await gs.update_pair(initial['session_id'],'p_0',{'answer':'Later acknowledged edit'})
                finally:release.set()
                result=await task;self.assertEqual(result['pairs_saved'],1)
        asyncio.run(run())
        self.assertEqual(json.loads((Path(self.tmp.name)/gs.EXPORT_DIRNAME/'snapshot.json').read_text())[0]['answer'],'Original')
        self.assertEqual(self.restart()['pairs'][0]['answer'],'Later acknowledged edit')

    def test_scan_of_missing_storage_does_not_create_directory(self):
        with tempfile.TemporaryDirectory() as missing:
            with patch.object(settings,'upload_dir',missing):
                self.assertEqual(gs.session_diagnostics(),[])
                self.assertFalse((Path(missing)/'goldstandard_sessions').exists())

    def test_storage_scan_failure_reports_without_destroying_cached_state(self):
        before=gs.get_session(self.data['session_id'])
        with patch.object(gs.os,'scandir',side_effect=OSError('Owned storage read failure')):
            gs.load_sessions_from_disk()
            self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_STORAGE_UNAVAILABLE')
        self.assertEqual(gs.get_session(self.data['session_id']),before)
        self.assertEqual(gs.session_diagnostics(),[])

    def test_model_failure_and_cancellation_status_are_persisted(self):
        async def run():
            initial=fixture();initial.update(status='generating',pairs=[],pairs_total=1,pairs_completed=0);gs.store_session(initial)
            with patch.object(gs,'_generate_pair',new=AsyncMock(side_effect=ValueError('Controlled model failure'))):await gs._run_generation(initial['session_id'],[{}])
            self.assertEqual(gs.get_session(initial['session_id'])['status'],'failed')
            initial['status']='generating';gs.store_session(initial);pending=asyncio.Event()
            async def wait(chunk):pending.set();await asyncio.Event().wait()
            with patch.object(gs,'_generate_pair',side_effect=wait):
                task=asyncio.create_task(gs._run_generation(initial['session_id'],[{}]));await pending.wait();task.cancel()
                with self.assertRaises(asyncio.CancelledError):await task
        asyncio.run(run());self.assertEqual(self.restart()['status'],'cancelled')

    def test_removed_read_and_temporary_issues_clear_but_write_failure_remains(self):
        root=gs._sessions_dir();bad=root/'gs_450bad00.json';temporary=root/'.gs_450abcde-owned.tmp'
        bad.write_text('{');temporary.write_text('unpublished');self.assertEqual(len(gs.session_diagnostics()),2)
        with patch.object(gs.os,'replace',side_effect=OSError('Owned replacement failure')):
            with self.assertRaises(gs.GoldStandardError):asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Rejected'}))
        bad.unlink();temporary.unlink();issues=gs.session_diagnostics()
        self.assertEqual([issue['code'] for issue in issues],['SESSION_WRITE_FAILED'])

    def test_scan_does_not_block_edit_or_publish_stale_read_failure(self):
        path=gs._session_path(self.data['session_id']);path.write_text('{')
        entered=threading.Event();release=threading.Event();original=Path.read_text
        def read(p,*args,**kwargs):
            text=original(p,*args,**kwargs)
            if p==path:
                entered.set()
                if not release.wait(5):raise AssertionError('Owned scan was not released')
            return text
        with ThreadPoolExecutor(max_workers=2) as pool,patch.object(Path,'read_text',read):
            scan=pool.submit(gs.session_diagnostics);self.assertTrue(entered.wait(5))
            edit=pool.submit(lambda:asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Committed during scan'})))
            try:self.assertEqual(edit.result(timeout=2)['answer'],'Committed during scan')
            finally:release.set()
            self.assertEqual(scan.result(timeout=5),[])
        self.assertEqual(self.restart()['pairs'][0]['answer'],'Committed during scan')

    def test_marker_failure_continues_other_sessions_without_primary_failure(self):
        second=fixture();second['session_id']='gs_450abcdf';gs.store_session(second);original=gs.os.replace
        def replace(src,dst):
            if Path(dst).stem==self.data['session_id']:raise OSError('Owned marker failure')
            return original(src,dst)
        with patch.object(gs.os,'replace',side_effect=replace):
            self.assertEqual(gs.mark_orphaned('OwnedPersistence','Owned completed deletion'),1)
        self.assertTrue(gs.get_session(self.data['session_id'])['orphaned'])
        self.assertNotIn('orphaned',json.loads(gs._session_path(self.data['session_id']).read_text()))
        self.assertTrue(gs.get_session(second['session_id'])['orphaned'])
        self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_WRITE_FAILED')

    def test_failed_orphan_marker_refuses_current_export(self):
        with patch.object(gs,'_save_session_sync',side_effect=OSError('Owned marker write fault')):
            self.assertEqual(gs.mark_orphaned('OwnedPersistence','Owned completed deletion'),0)
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned-review') as client:
                return await client.post('/goldstandard/save',json={'session_id':self.data['session_id'],'filename':'owned-review.json'})
        response=asyncio.run(request())
        self.assertEqual(response.status_code,409,response.text)
        self.assertEqual(response.json()['error']['code'],'HISTORICAL_SESSION')
        self.assertFalse((Path(self.tmp.name)/gs.EXPORT_DIRNAME/'owned-review.json').exists())

    def test_generation_commit_failure_retains_code_after_followup_status_writes(self):
        async def run():
            initial=fixture();initial.update(status='generating',pairs=[],pairs_total=1,pairs_completed=0);gs.store_session(initial)
            original=gs.os.replace;calls=0
            def replace(src,dst):
                nonlocal calls
                calls+=1
                if calls==1:raise OSError('Owned first pair commit failure')
                return original(src,dst)
            with patch.object(gs,'_generate_pair',new=AsyncMock(return_value=fixture()['pairs'][0])),patch.object(gs.os,'replace',side_effect=replace):
                with self.assertRaises(gs.GoldStandardError):await gs._run_generation(initial['session_id'],[{}])
                await gs._record_generation_failure(initial['session_id'],gs.GoldStandardError('SESSION_WRITE_FAILED','Session update could not be persisted. The previous snapshot is unchanged.',503))
        asyncio.run(run());state=self.restart();self.assertEqual(state['status'],'failed')
        self.assertEqual(state['persistence_error']['code'],'SESSION_WRITE_FAILED');self.assertIn('SESSION_WRITE_FAILED',state['errors'][0])
        self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_WRITE_FAILED')

    def test_generation_failure_reporter_keeps_event_loop_responsive(self):
        async def run():
            entered=threading.Event();release=threading.Event();original=gs._update_session_sync
            def delayed(sid,change):
                entered.set()
                if not release.wait(5):raise AssertionError('Owned failure reporter was not released')
                return original(sid,change)
            with patch.object(gs,'_update_session_sync',side_effect=delayed):
                task=asyncio.create_task(gs._record_generation_failure(self.data['session_id'],RuntimeError('Owned unexpected failure')))
                self.assertTrue(await asyncio.to_thread(entered.wait,5))
                try:await asyncio.wait_for(asyncio.sleep(0.02),0.5)
                finally:release.set()
                await task
        asyncio.run(run());self.assertEqual(self.restart()['status'],'failed')

    async def generate(self,pair):
        # The real task, done-callback and reporter run; only the model and sampling are owned.
        chunk={'content':'Inert','source_file':'inert.txt','chunk_index':0}
        with patch.object(gs.wc,'_sample_chunks_sync',new=MagicMock(return_value=[chunk])),patch.object(gs,'_generate_pair',side_effect=pair),patch.object(gs,'_tasks',set()):
            sid=(await gs.start_generation('OwnedPersistence',1,None))['session_id']
            deadline=time.monotonic()+5
            while gs._tasks:
                if time.monotonic()>deadline:raise AssertionError('Owned generation did not finish')
                await asyncio.sleep(0.01)
        return sid

    async def fetch(self,sid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned-review') as client:
            return await client.get('/goldstandard/session/'+sid)

    def test_persistent_replace_failure_ends_generation_failed(self):
        fault=threading.Event();original=gs.os.replace
        def replace(src,dst):
            if fault.is_set():raise OSError('Owned persistent replace fault')
            return original(src,dst)
        async def pair(chunk):fault.set();return fixture()['pairs'][0]
        async def run():
            with patch.object(gs.os,'replace',side_effect=replace):
                sid=await self.generate(pair);return sid,await self.fetch(sid)
        sid,response=asyncio.run(run());state=gs.get_session(sid)
        self.assertEqual(state['status'],'failed')
        self.assertTrue(any('SESSION_WRITE_FAILED' in e for e in state['errors']),state['errors'])
        self.assertEqual(state['persistence_error']['code'],'SESSION_WRITE_FAILED')
        self.assertEqual((response.status_code,response.json()['status']),(200,'failed'),response.text)
        self.assertEqual(json.loads(gs._session_path(sid).read_text())['status'],'generating')

    def test_directory_at_session_path_ends_generation_failed(self):
        async def pair(chunk):
            # The only generating session is the one this case started.
            sid=next(key for key,value in gs._sessions.items() if value['status']=='generating')
            path=gs._session_path(sid);path.unlink();path.mkdir()
            return fixture()['pairs'][0]
        async def run():
            sid=await self.generate(pair);return sid,await self.fetch(sid)
        sid,response=asyncio.run(run());state=gs.get_session(sid)
        self.assertEqual(state['status'],'failed')
        self.assertEqual((response.status_code,response.json()['status']),(200,'failed'),response.text)
        self.assertTrue(any('SESSION_WRITE_FAILED' in e for e in state['errors']),state['errors'])

    def test_cancelled_generation_with_failing_final_write_ends_failed(self):
        fault=threading.Event();original=gs.os.replace
        def replace(src,dst):
            if fault.is_set():raise OSError('Owned persistent replace fault')
            return original(src,dst)
        async def run():
            waiting=asyncio.Event()
            async def pair(chunk):fault.set();waiting.set();await asyncio.Event().wait()
            async def cancel():
                await waiting.wait()
                for task in list(gs._tasks):task.cancel()
            with patch.object(gs.os,'replace',side_effect=replace):
                canceller=asyncio.create_task(cancel())
                sid=await self.generate(pair);await canceller;return sid
        self.assertEqual(gs.get_session(asyncio.run(run()))['status'],'failed')

    def test_cache_fallback_keeps_event_loop_responsive(self):
        initial=fixture();initial.update(session_id='gs_450abcd1',status='generating',pairs=[],pairs_total=1,pairs_completed=0);gs.store_session(initial)
        written=threading.Event();held=threading.Event();release=threading.Event();seen=[]
        def failing(sid,change):
            written.set();raise gs.GoldStandardError('SESSION_WRITE_FAILED','Owned persistent write fault',503)
        def hold():
            with gs._state_lock:
                held.set();seen.append(release.wait(5))
        holder=threading.Thread(target=hold);holder.start();self.assertTrue(held.wait(5))
        async def run():
            with patch.object(gs,'_update_session_sync',side_effect=failing):
                task=asyncio.create_task(gs._record_generation_failure(initial['session_id'],RuntimeError('Owned unexpected failure')))
                try:
                    self.assertTrue(await asyncio.to_thread(written.wait,5))
                    # Long enough for the reporter to reach its fallback; on the loop it would block here.
                    await asyncio.sleep(0.05)
                finally:release.set()
                await task
        try:asyncio.run(run())
        finally:release.set();holder.join(5)
        self.assertEqual(seen,[True])
        self.assertEqual(gs.get_session(initial['session_id'])['status'],'failed')

    def test_uncertain_failure_report_keeps_both_reasons(self):
        original=gs.GoldStandardError('SESSION_WRITE_FAILED','Original generation write failed',500)
        report=gs.GoldStandardError('SESSION_DURABILITY_UNCERTAIN','Reporter replace completed but sync failed',500)
        gs._publish_generation_failure(self.data['session_id'],original,report)
        current=gs.get_session(self.data['session_id'])
        self.assertEqual(current['persistence_error']['code'],'SESSION_DURABILITY_UNCERTAIN')
        self.assertTrue(any('SESSION_WRITE_FAILED' in message for message in current['errors']))
        self.assertTrue(any('SESSION_DURABILITY_UNCERTAIN' in message for message in current['errors']))

    def test_cached_generation_failure_is_saved_by_next_successful_write(self):
        # Reviewer (#136 spec sentence): the cache runs ahead of disk until a later successful write saves it.
        fault=threading.Event();original=gs.os.replace
        def replace(src,dst):
            if fault.is_set():raise OSError('Owned persistent replace fault')
            return original(src,dst)
        async def pair(chunk):fault.set();return fixture()['pairs'][0]
        async def run():
            with patch.object(gs.os,'replace',side_effect=replace):return await self.generate(pair)
        sid=asyncio.run(run())
        self.assertEqual(json.loads(gs._session_path(sid).read_text())['status'],'generating')
        self.assertEqual(gs.mark_stale('OwnedPersistence','Owned later write'),2)
        gs._sessions={};gs.load_sessions_from_disk();state=gs.get_session(sid)
        self.assertEqual(state['status'],'failed',state)
        self.assertEqual(state['persistence_error']['code'],'SESSION_WRITE_FAILED')
        self.assertTrue(state['stale'])

    def test_cache_fallback_does_not_recreate_a_removed_session(self):
        # Reviewer: a session gone from the cache must not be resurrected by the fallback.
        before=gs._store_revision
        gs._publish_generation_failure('gs_450abcd2',RuntimeError('Owned failure'),OSError('Owned write fault'))
        self.assertIsNone(gs.get_session('gs_450abcd2'));self.assertEqual(gs._store_revision,before)

    def test_tuning_aborts_when_both_historical_marker_writes_fail(self):
        from services import tuning
        job={'status':'queued'};jobid='owned-marker-job'
        with patch.dict(tuning._jobs,{jobid:job}),patch.object(tuning.sources,'has_sources',return_value=False),patch.object(tuning,'_existing_chunks',return_value=[{'content':'Inert'}]),patch.object(tuning,'_rebuild',side_effect=lambda *args,**kwargs:(kwargs['before_replace'](),1)[1]) as rebuild,patch.object(gs.os,'replace',side_effect=OSError('Owned marker failure')):
            tuning._run(jobid,'OwnedPersistence','reembed',{})
        rebuild.assert_called_once();self.assertEqual(job['status'],'failed');self.assertEqual(job.get('chunks_written',0),0)
        self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_WRITE_FAILED')


    def test_replace_import_continues_after_marker_failure_and_reports_retention_truthfully(self):
        from services import importer
        job={'status':'queued'};jobid='owned-import-marker-job';pkg=Path(self.tmp.name)
        manifest={'collection':{'name':'OwnedPersistence','chunk_count':1}}
        client=MagicMock();client.collections.exists.side_effect=lambda name:name=='OwnedPersistence'
        original_replace=gs.os.replace
        def failed_marker(src,dst):
            if Path(dst).stem==self.data['session_id']:raise OSError('Owned marker failure')
            return original_replace(src,dst)
        def deleted(name):
            gs.mark_orphaned(name,'Owned completed primary replacement')
            return 1
        with ExitStack() as stack:
            stack.enter_context(patch.dict(importer._jobs,{jobid:job}))
            for obj,name,kwargs in [
                (importer.packager,'exports_dir',{'return_value':pkg}),
                (importer.packager,'open_package',{'return_value':(pkg,manifest)}),
                (importer.packager,'verify_digests',{'return_value':None}),
                (importer.packager,'sha256_file',{'return_value':'01234567'*8}),
                (importer,'_check_embedding',{'return_value':None}),
                (importer,'_ensure_models',{'return_value':[]}),
                (importer,'_restore_sidecars',{'return_value':[]}),
                (importer,'_mark_started',{'return_value':None}),
                (importer,'_mark_finished',{'return_value':None}),
                (importer.wc,'_collection_exists_sync',{'side_effect':lambda name:name=='OwnedPersistence'}),
                (importer.wc,'_delete_collection_sync',{'side_effect':deleted}),
                (importer.wc,'get_client',{'return_value':client}),
                (gs.os,'replace',{'side_effect':failed_marker})]:
                stack.enter_context(patch.object(obj,name,**kwargs))
            build=stack.enter_context(patch.object(importer,'_build',return_value=1))
            importer._run(jobid,'owned-package.tar.gz','replace')
        self.assertEqual(build.call_count,2);self.assertEqual(job['status'],'completed')
        self.assertIn('inspect session recovery diagnostics',job['notes'][0]);self.assertNotIn('marked orphaned',job['notes'][0])
        self.assertEqual(gs.session_diagnostics()[0]['code'],'SESSION_WRITE_FAILED')

    # #127 follow-ups.
    def pending(self):
        return Path(self.tmp.name)/'goldstandard_sessions'/'pending_markers'/(self.data['session_id']+'.json')

    def fail_session_file_replace(self):
        path=gs._session_path(self.data['session_id']);original=gs.os.replace
        def replace(src,dst):
            if Path(dst)==path:raise OSError('Owned session-file marker fault')
            return original(src,dst)
        return patch.object(gs.os,'replace',side_effect=replace)

    def test_session_poll_does_not_block_event_loop_during_slow_commit(self):
        async def run():
            inside=threading.Event();real_fsync=os.fsync
            def slow_fsync(fd):
                inside.set();time.sleep(1.5);return real_fsync(fd)
            with patch.object(gs.os,'fsync',side_effect=slow_fsync):
                writer=asyncio.create_task(gs.update_pair(self.data['session_id'],'p_0',{'status':'edited','answer':'Slow commit'}))
                self.assertTrue(await asyncio.to_thread(inside.wait,5))
                gaps=[]
                async def ticker():
                    last=time.monotonic()
                    for _ in range(30):
                        await asyncio.sleep(0.02);now=time.monotonic();gaps.append(now-last);last=now
                tick=asyncio.create_task(ticker());await asyncio.sleep(0.05)
                async with client() as c:response=await c.get('/goldstandard/session/'+self.data['session_id'])
                await tick;await writer
            self.assertEqual(response.status_code,200)
            self.assertLess(max(gaps),0.5,'event loop stalled %.2fs while a poll waited for the writer lock'%max(gaps))
        asyncio.run(run())

    def test_failed_marker_survives_restart_and_refuses_current_export(self):
        first="""import sys
from unittest.mock import patch
from config import settings
from services import goldstandard as gs
settings.upload_dir=sys.argv[1]
gs.load_sessions_from_disk()
with patch.object(gs,'_save_session_sync',side_effect=OSError('Owned marker write fault')):
    gs.mark_orphaned('OwnedPersistence','Owned completed deletion')
"""
        second="""import sys,asyncio,json,httpx
from config import settings
from main import app
from services import goldstandard as gs
settings.upload_dir=sys.argv[1]
gs.load_sessions_from_disk()
async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned-review') as c:
        r=await c.post('/goldstandard/save',json={'session_id':'gs_450abcde','filename':'owned-restart.json'})
        return r.status_code,r.json()
print(json.dumps({'export':list(asyncio.run(run())),'diagnostics':gs.session_diagnostics()}))
"""
        one=subprocess.run([sys.executable,'-c',first,self.tmp.name],env=child_env(),capture_output=True,text=True,timeout=60)
        self.assertEqual(one.returncode,0,one.stderr[-2000:])
        two=subprocess.run([sys.executable,'-c',second,self.tmp.name],env=child_env(),capture_output=True,text=True,timeout=60)
        self.assertEqual(two.returncode,0,two.stderr[-2000:])
        observed=json.loads(two.stdout.strip().splitlines()[-1])
        self.assertEqual(observed['export'][0],409,observed)
        self.assertEqual(observed['export'][1]['error']['code'],'HISTORICAL_SESSION')
        issues=[i for i in observed['diagnostics'] if i['filename']=='gs_450abcde.json']
        self.assertEqual([i['code'] for i in issues],['SESSION_WRITE_FAILED'],observed['diagnostics'])
        self.assertIn('pending marker',issues[0]['message'])

    def test_failed_marker_is_applied_to_package_snapshots(self):
        with self.fail_session_file_replace():
            self.assertEqual(gs.mark_stale('OwnedPersistence','Owned rebuild'),0)
        self.assertNotIn('stale',json.loads(gs._session_path(self.data['session_id']).read_text()))
        exported=[s for s in gs._sessions_on_disk() if s['session_id']==self.data['session_id']]
        self.assertTrue(exported[0].get('stale'));self.assertEqual(exported[0]['stale_reason'],'Owned rebuild')
        self.assertNotIn('stale',json.loads(gs._session_path(self.data['session_id']).read_text()))

    def test_next_successful_write_persists_marker_and_removes_pending_file(self):
        with self.fail_session_file_replace():
            gs.mark_orphaned('OwnedPersistence','Owned completed deletion')
        self.assertTrue(self.pending().is_file())
        asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Later edit'}))
        self.assertTrue(json.loads(gs._session_path(self.data['session_id']).read_text())['orphaned'])
        self.assertFalse(self.pending().exists());self.assertEqual(gs.session_diagnostics(),[])

    def test_both_failed_markers_are_kept_pending(self):
        with self.fail_session_file_replace():
            gs.mark_stale('OwnedPersistence','Owned rebuild');gs.mark_orphaned('OwnedPersistence','Owned deletion')
        state=self.restart();self.assertTrue(state['stale']);self.assertTrue(state['orphaned'])
        self.assertEqual((state['stale_reason'],state['orphaned_reason']),('Owned rebuild','Owned deletion'))

    def test_unreadable_pending_marker_is_preserved_and_reported(self):
        self.pending().parent.mkdir();self.pending().write_text('{')
        state=self.restart();self.assertFalse(state.get('orphaned'))
        self.assertEqual([(i['filename'],i['code']) for i in gs.session_diagnostics()],[('pending_markers/gs_450abcde.json','SESSION_READ_FAILED')])
        asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Later edit'}));self.assertEqual(self.pending().read_text(),'{')

    def test_pending_marker_write_failure_keeps_guard_until_restart(self):
        with patch.object(gs.os,'replace',side_effect=OSError('Owned storage fault')):
            self.assertEqual(gs.mark_orphaned('OwnedPersistence','Owned completed deletion'),0)
        self.assertFalse(self.pending().exists())
        with self.assertRaises(gs.GoldStandardError) as error:asyncio.run(gs.save_session(self.data['session_id'],'owned.json'))
        self.assertEqual(error.exception.code,'HISTORICAL_SESSION')
        issue=gs.session_diagnostics()[0];self.assertEqual(issue['code'],'SESSION_WRITE_FAILED');self.assertIn('until restart',issue['message'])

    def test_unknown_persistence_error_does_not_fail_writes_or_markers(self):
        for value in ({'code':'BOGUS','message':'Owned'},'Owned text'):
            with self.subTest(value=value):
                data=fixture();data['persistence_error']=value;gs.store_session(data)
                self.assertEqual(gs.session_diagnostics(),[])
                self.assertEqual(asyncio.run(gs.update_pair(data['session_id'],'p_0',{'answer':'Edit'}))['answer'],'Edit')
                self.assertEqual(gs.mark_orphaned('OwnedPersistence','Owned deletion'),1)

    def test_import_restore_strips_persistence_error(self):
        from services import importer
        source=fixture();source.update(session_id='gs_450abcd9',errors=['Owned kept reason'],persistence_error={'code':'SESSION_WRITE_FAILED','message':'Owned source failure'})
        pkg=Path(self.tmp.name)/'owned-package';pkg.mkdir();restored=[]
        importer._restore_sidecars('OwnedPersistence',pkg,'OwnedPersistence',[source],restored)
        stored=gs.get_session(restored[0]['session_id'])
        self.assertNotIn('persistence_error',stored);self.assertEqual(stored['errors'],['Owned kept reason'])
        self.assertNotIn('persistence_error',json.loads(gs._session_path(stored['session_id']).read_text()))
        self.assertEqual(gs.session_diagnostics(),[])

    def test_nonregular_session_destination_is_typed_write_failure(self):
        path=gs._session_path(self.data['session_id']);path.unlink();path.mkdir()
        async def run():
            async with client() as c:
                return await c.patch('/goldstandard/session/'+self.data['session_id']+'/pair/p_0',json={'status':'edited','answer':'Refused'})
        response=asyncio.run(run())
        self.assertEqual(response.status_code,503,response.text);self.assertEqual(response.json()['error']['code'],'SESSION_WRITE_FAILED')
        self.assertEqual(gs.get_session(self.data['session_id']),self.data)

    def test_cached_read_failure_uses_one_absolute_diagnostic_key(self):
        sid='gs_450bad05';(gs._sessions_dir()/(sid+'.json')).write_text('{')
        gs._sessions[sid]={'session_id':sid,'collection':'OwnedPersistence'}
        self.assertEqual(len(gs.sessions_for('OwnedPersistence')),1)
        self.assertTrue(all(Path(key).is_absolute() for key in gs._diagnostics),list(gs._diagnostics))
        names=[i['filename'] for i in gs.session_diagnostics()]
        self.assertEqual(names,[sid+'.json'])

    def test_marker_failure_wording_differs_from_edit_failure(self):
        with self.fail_session_file_replace():
            with self.assertRaises(gs.GoldStandardError):asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Rejected'}))
            self.assertIn('previous snapshot remains authoritative',gs.session_diagnostics()[0]['message'])
            gs.mark_orphaned('OwnedPersistence','Owned completed deletion')
        issue=gs.session_diagnostics()[0];self.assertEqual(issue['code'],'SESSION_WRITE_FAILED')
        self.assertIn('pending marker',issue['message']);self.assertNotIn('previous snapshot remains authoritative',issue['message'])

    def test_scan_cache_skips_unchanged_and_reflects_changes(self):
        path=gs._session_path(self.data['session_id']);original=Path.read_text;reads=[]
        def read(p,*args,**kwargs):
            if p==path:reads.append(1)
            return original(p,*args,**kwargs)
        with patch.object(Path,'read_text',read):
            gs.session_diagnostics();reads.clear()
            gs.session_diagnostics();self.assertEqual(reads,[],'an unchanged session file was read again')
            changed=fixture();changed['pairs'][0]['answer']='Changed';gs.store_session(changed)
            gs._sessions={};gs.load_sessions_from_disk();self.assertEqual(len(reads),1)
            self.assertEqual(gs.get_session(self.data['session_id'])['pairs'][0]['answer'],'Changed')
        bad=gs._sessions_dir()/'gs_450bad00.json';bad.write_text('{')
        self.assertEqual(len(gs.session_diagnostics()),1);bad.unlink();self.assertEqual(gs.session_diagnostics(),[])

    # Reviewer additions for #127.
    def test_session_and_pending_marker_files_are_owner_only(self):
        self.assertEqual(gs._session_path(self.data['session_id']).stat().st_mode&0o777,0o600)
        with self.fail_session_file_replace():
            gs.mark_orphaned('OwnedPersistence','Owned completed deletion')
        self.assertEqual(self.pending().stat().st_mode&0o777,0o600)

    def test_pending_marker_after_restart_is_saved_by_next_write(self):
        with self.fail_session_file_replace():
            gs.mark_orphaned('OwnedPersistence','Owned completed deletion')
        state=self.restart();self.assertTrue(state['orphaned']);self.assertTrue(self.pending().is_file())
        asyncio.run(gs.update_pair(self.data['session_id'],'p_0',{'answer':'Later edit'}))
        stored=json.loads(gs._session_path(self.data['session_id']).read_text())
        self.assertTrue(stored['orphaned']);self.assertEqual(stored['orphaned_reason'],'Owned completed deletion')
        self.assertFalse(self.pending().exists());self.assertEqual(gs.session_diagnostics(),[])

    def test_symlinked_pending_storage_is_refused_and_guard_holds(self):
        outside=Path(self.tmp.name)/'outside';outside.mkdir()
        self.pending().parent.symlink_to(outside,target_is_directory=True)
        with self.fail_session_file_replace():
            self.assertEqual(gs.mark_orphaned('OwnedPersistence','Owned completed deletion'),0)
        self.assertEqual(list(outside.iterdir()),[])
        with self.assertRaises(gs.GoldStandardError) as error:asyncio.run(gs.save_session(self.data['session_id'],'owned.json'))
        self.assertEqual(error.exception.code,'HISTORICAL_SESSION')
        issues={i['filename']:i for i in gs.session_diagnostics()}
        self.assertIn('until restart',issues['gs_450abcde.json']['message'])
        self.assertEqual(issues['pending_markers']['code'],'SESSION_READ_FAILED')

    def test_pending_marker_without_session_file_is_ignored(self):
        self.pending().parent.mkdir()
        (self.pending().parent/'gs_450dead0.json').write_text(json.dumps({'session_id':'gs_450dead0','orphaned':True,'orphaned_reason':'Owned','orphaned_at':None}))
        self.restart();self.assertIsNone(gs.get_session('gs_450dead0'));self.assertEqual(gs.session_diagnostics(),[])

    def test_durability_uncertain_marker_keeps_its_own_wording(self):
        with patch.object(gs,'_sync_directory',side_effect=OSError('Owned directory sync fault')):
            gs.mark_orphaned('OwnedPersistence','Owned completed deletion')
        self.assertTrue(json.loads(gs._session_path(self.data['session_id']).read_text())['orphaned'])
        issue=gs.session_diagnostics()[0];self.assertEqual(issue['code'],'SESSION_DURABILITY_UNCERTAIN')
        self.assertNotIn('pending marker',issue['message'])

if __name__ == '__main__':
    unittest.main()
