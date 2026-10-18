"""Owned real backend, concurrent HTTP edits and fresh-process filesystem reload.

No startup sweep or model contact. Generated pairs are controlled, while the
collection/sample reads, HTTP handlers and filesystem durability are real.
"""
import asyncio,copy,json,os,subprocess,sys,tempfile,uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from config import settings
from main import app
from services import goldstandard as gs,weaviate_client as wc

collection=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Persistence'+uuid.uuid4().hex[:10]
created=False
with tempfile.TemporaryDirectory(prefix='session-persistence-live-') as directory,patch.object(settings,'upload_dir',directory):
    async def run():
        global created
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned-fixture') as client:
            created=True
            response=await client.post('/collections',json={'name':collection});assert response.status_code==201,response.text
            coll=wc.get_client().collections.get(collection)
            for i in range(2):coll.data.insert(properties={'content':'Owned inert chunk'+str(i),'source_file':'inert.txt','chunk_index':i},vector=[0.1]*768)
            print('PASS unique real collection and supplied-vector chunks created',flush=True)
            pending=asyncio.Event();release=asyncio.Event();calls=0
            async def generated(chunk):
                nonlocal calls
                calls+=1
                if calls==2:pending.set();await release.wait()
                return {'pair_id':'p_owned'+str(calls),'question':'Original question','answer':'Original answer','contexts':[chunk['content']],'ground_truth':'Original truth','source_file':'inert.txt','chunk_index':calls-1,'status':'pending'}
            with patch.object(gs,'_generate_pair',side_effect=generated):
                started=await client.post('/goldstandard/generate',json={'collection':collection,'sample_size':2});assert started.status_code==202,started.text
                sid=started.json()['session_id'];await asyncio.wait_for(pending.wait(),10)
                tasks=list(gs._tasks)
                responses=await asyncio.gather(*[client.patch('/goldstandard/session/'+sid+'/pair/p_owned1',json={'status':'edited',key:value}) for key,value in [('question','Acknowledged question'),('answer','Acknowledged answer'),('ground_truth','Acknowledged truth')]])
                assert all(response.status_code==200 for response in responses),[response.text for response in responses]
                await asyncio.to_thread(gs.mark_stale,collection,'Owned concurrent history flag')
                release.set();await asyncio.gather(*tasks)
            response=await client.get('/goldstandard/session/'+sid);assert response.status_code==200,response.text
            state=gs.get_session(sid);pair=state['pairs'][0]
            assert (pair['question'],pair['answer'],pair['ground_truth'])==('Acknowledged question','Acknowledged answer','Acknowledged truth')
            assert len(state['pairs'])==2 and state['stale'] and state['status']=='completed'
            print('PASS concurrent acknowledged HTTP edits and generation/history interleaving retained',flush=True)
            code="from config import settings;from services import goldstandard as gs;import sys,json;settings.upload_dir=sys.argv[1];gs.load_sessions_from_disk();print(json.dumps(gs.get_session(sys.argv[2])))"
            reloaded=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code,directory,sid],capture_output=True,text=True,check=True)
            assert json.loads(reloaded.stdout)==state,reloaded.stdout
            print('PASS fresh API process reload retains every acknowledged update and flag',flush=True)
            before=copy.deepcopy(state)
            with patch.object(gs.os,'replace',side_effect=OSError('Owned pre-replace fault')):
                failed=await client.patch('/goldstandard/session/'+sid+'/pair/p_owned1',json={'status':'edited','answer':'Rejected edit'})
            assert failed.status_code==503 and failed.json()['error']['code']=='SESSION_WRITE_FAILED',failed.text
            assert gs.get_session(sid)==before
            issues=await client.get('/goldstandard/diagnostics');assert issues.status_code==200 and issues.json()['issues'][0]['code']=='SESSION_WRITE_FAILED',issues.text
            print('PASS failed HTTP write is not acknowledged and diagnostic names failed snapshot',flush=True)
            pending=asyncio.Event();release=asyncio.Event()
            async def regenerate(chunk):pending.set();await release.wait();return {**before['pairs'][0],'answer':'Generated overwrite'}
            with patch.object(gs,'_generate_pair',side_effect=regenerate):
                task=asyncio.create_task(client.post('/goldstandard/regenerate',json={'session_id':sid,'pair_id':'p_owned1'}));await asyncio.wait_for(pending.wait(),10)
                edit=await client.patch('/goldstandard/session/'+sid+'/pair/p_owned1',json={'status':'edited','answer':'Latest acknowledged answer'});assert edit.status_code==200,edit.text
                release.set();conflict=await task
            assert conflict.status_code==409 and conflict.json()['error']['code']=='PAIR_CHANGED_DURING_REGENERATION',conflict.text
            assert gs.get_session(sid)['pairs'][0]['answer']=='Latest acknowledged answer'
            print('PASS in-flight regeneration rejects changed target instead of losing acknowledged edit',flush=True)
            # Marker persistence is secondary to an already completed deletion.
            original_replace=gs.os.replace
            def marker_fault(src,dst):
                if Path(dst).stem==sid:raise OSError('Owned deletion marker failure')
                return original_replace(src,dst)
            with patch.object(gs.os,'replace',side_effect=marker_fault):
                deleted=await client.delete('/collections/'+collection+'?confirm=true')
            assert deleted.status_code==200 and deleted.json()['objects_deleted']==2,deleted.text
            assert not wc._collection_exists_sync(collection)
            from routers import collections as collection_routes
            assert collection not in collection_routes._load_registry()
            assert any(issue['filename']==sid+'.json' and issue['code']=='SESSION_WRITE_FAILED' for issue in gs.session_diagnostics())
            print('PASS completed real HTTP deletion remains200 and registry removal completes despite marker persistence failure',flush=True)
            corrupt=gs._sessions_dir()/('gs_'+uuid.uuid4().hex[:8]+'.json');corrupt.write_bytes(b'{owned incomplete snapshot')
            response=await client.get('/goldstandard/diagnostics')
            assert any(issue['filename']==corrupt.name and issue['code']=='SESSION_READ_FAILED' for issue in response.json()['issues']),response.text
            assert corrupt.read_bytes()==b'{owned incomplete snapshot'
            print('PASS real unreadable file preserved and reported through diagnostic HTTP endpoint',flush=True)
            corrupt.unlink()
            response=await client.get('/goldstandard/diagnostics')
            assert not any(issue['filename']==corrupt.name for issue in response.json()['issues'])
            print('PASS removed unreadable file clears its recovery issue while failed-write diagnostic remains',flush=True)
    try:asyncio.run(run())
    finally:
        if created and wc._collection_exists_sync(collection):wc._delete_collection_sync(collection)
        for sid in [sid for sid,s in gs._sessions.items() if s.get('collection')==collection]:gs._sessions.pop(sid,None)
        wc.close_client()
assert not wc._collection_exists_sync(collection);wc.close_client()
print('PASS owned collection/session/files removed',flush=True)
