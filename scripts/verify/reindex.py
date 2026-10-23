"""Real Weaviate reindex with an unreachable embedding endpoint; owned fixtures only."""
import asyncio, json, os, subprocess, sys, tempfile, threading, uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from config import settings
from main import app
from services import goldstandard as gs, tuning, ingest_pipeline as ingest
from services import weaviate_client as wc


async def completed(client,path,timeout=300,expected_status="completed"):
    deadline=asyncio.get_running_loop().time()+timeout;last='not observed'
    while True:
        remaining=deadline-asyncio.get_running_loop().time()
        if remaining<=0:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}')
        try:response=await asyncio.wait_for(client.get(path),remaining)
        except asyncio.TimeoutError as exc:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}') from exc
        assert response.status_code==200,response.text
        job=response.json();last=job.get('status','missing')
        if last not in ('queued','running'):
            assert last==expected_status,job
            return job
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def settle_owned_jobs(jobs,timeout=30):
    deadline=asyncio.get_running_loop().time()+timeout
    while True:
        pending=[(module.__name__,jobid,(module.get_job(jobid) or {}).get('status','missing')) for module,jobid in jobs if (module.get_job(jobid) or {}).get('status') not in ('completed','failed')]
        if not pending or asyncio.get_running_loop().time()>=deadline:return pending
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def bounded_poll_cases():
    from types import SimpleNamespace
    class StuckClient:
        async def get(self,path):return SimpleNamespace(status_code=200,text='',json=lambda:{'status':'running'})
    try:await completed(StuckClient(),'/owned/stuck-job',timeout=.01)
    except TimeoutError as exc:assert '/owned/stuck-job' in str(exc) and 'running' in str(exc)
    else:raise AssertionError('Stuck owned job did not meet its deadline')
    pending=await settle_owned_jobs([(SimpleNamespace(__name__='owned',get_job=lambda identity:{'status':'queued'}),'owned-cleanup-job')],timeout=.01)
    assert pending==[('owned','owned-cleanup-job','queued')]
    print('PASS two controlled job-poll and cleanup deadline cases',flush=True)

def owned_name(prefix,token):
    # Parent suites may sweep their prefix. This standalone owner must remain
    # outside that namespace when preserving a failed or interrupted fixture.
    if not prefix:raise ValueError('A nonempty parent fixture prefix is required')
    first = 'A' if not prefix.startswith('A') else 'B'
    return first+'OwnedReindex'+token

def preserve_receipt(directory,created,pending):
    path=Path(directory)/'owned-fixtures.json'
    with path.open('w') as output:
        json.dump({'created_collections':list(created),'pending_jobs':pending,'fixture_directory':directory,'cleanup':'Inspect terminal writer state and delete only these exact owned names; do not prefix-sweep.'},output,indent=2)
        output.flush();os.fsync(output.fileno())
    return str(path)


async def queued_ingest_checks(api,client,collection,jobs,check):
    before=await asyncio.to_thread(tuning._existing_records,collection)
    paused=threading.Event();release=threading.Event();original_verify=tuning._verify_records;once=False
    def verify(target,records):
        nonlocal once
        original_verify(target,records)
        if target==collection and not once:
            once=True;paused.set()
            if not release.wait(30):raise TimeoutError('Owned source-check pause expired')
    # Only the embedding fixture is replaced; the HTTP upload, parser, chunker,
    # ingest worker, source retention and actual backend writes remain real.
    def supplied_vectors(target,chunks):
        col=client.collections.get(target)
        for chunk in chunks:col.data.insert(properties=chunk,uuid=str(uuid.uuid4()),vector=[.25]*len(before[0]['vector']))
    with patch.object(tuning,'_verify_records',side_effect=verify),patch.object(wc,'_insert_chunks_sync',side_effect=supplied_vectors):
        try:
            response=await api.post('/tune/reindex',json={'collection':collection,'index_type':'hnsw','distance_metric':'cosine'})
            assert response.status_code==202,response.text;reindex=response.json()['job_id'];jobs.append((tuning,reindex))
            check(await asyncio.to_thread(paused.wait,10),'real reindex reaches protected source check before cutover')
            uploaded=await api.post('/ingest/upload',data={'collection':collection,'strategy':'fixed','chunk_size':'150','chunk_overlap':'0','min_chunk_size':'100'},files={'files':('owned-concurrent.txt',b'Owned concurrency upload with inert public text. '*40,'text/plain')})
            assert uploaded.status_code==202,uploaded.text;upload=uploaded.json()['job_id'];jobs.append((ingest,upload))
            status=await api.get('/ingest/job/'+upload)
            check(status.status_code==200 and status.json()['status']=='queued','actual ingest HTTP job remains queued while reindex holds the source guard')
            release.set()
            reindexed=await completed(api,'/tune/job/'+reindex)
            ingested=await completed(api,'/ingest/job/'+upload)
            check(reindexed['chunks_written']==len(before) and ingested['chunks_stored']>0,'reindex verifies its copy before the waiting real ingest completes')
        finally:release.set()
    after=await asyncio.to_thread(tuning._existing_records,collection);observed={record['id']:record for record in after}
    check(all(observed.get(record['id'])==record for record in before) and len(after)==len(before)+ingested['chunks_stored'],'real backend retains original exact records and the post-cutover upload')


async def retained_cutover_checks(api,client,name,temp,record_create,jobs,check):
    collection=name+'CutoverFail';sid='gs_'+uuid.uuid4().hex[:8]
    await asyncio.to_thread(wc._create_collection_sync,collection,'hnsw','cosine',{})
    col=client.collections.get(collection)
    await asyncio.to_thread(col.data.insert,properties={'content':'Owned inert recovery','source_file':'owned.txt','chunk_index':0},uuid=str(uuid.uuid4()),vector=[.125]*768)
    before=await asyncio.to_thread(tuning._existing_records,collection)
    # Sessions are stored under the collection's stored name (#372); the request below
    # still uses the lowercase-first spelling, which the API resolves to it.
    session={'session_id':sid,'collection':collection,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs_attempted':1,'pairs_failed':0,'pairs':[{'pair_id':'p_owned','question':'Owned','answer':'Owned','contexts':['Owned inert recovery'],'ground_truth':'Owned','source_file':'owned.txt','chunk_index':0,'status':'approved'}]}
    await asyncio.to_thread(gs.store_session,session);session_bytes=await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes())
    def create(target,*args,**kwargs):
        if target==collection:raise RuntimeError('Owned injected final-create failure after cutover')
        record_create(target,*args,**kwargs)
    with patch.object(wc,'_create_collection_sync',side_effect=create):
        response=await api.post('/tune/reindex',json={'collection':collection[:1].lower()+collection[1:],'index_type':'flat','distance_metric':'dot'})
        assert response.status_code==202,response.text;job=response.json()['job_id'];jobs.append((tuning,job))
        result=await completed(api,'/tune/job/'+job,expected_status='failed')
    check(result['chunks_written']==0,'post-cutover failure is not published as completion')
    stage=result['error_detail']['recovered_as'];check(stage.startswith(collection+'__tuning_'),'failure identifies the exact operation-owned retained copy')
    check(await asyncio.to_thread(tuning._existing_records,stage)==before,'retained backend copy preserves exact UUID/properties/vector after final create fails')
    paths=await asyncio.to_thread(lambda:list(tuning.collection_recovery._root().glob('*.json')));assert len(paths)==1
    owner=await asyncio.to_thread(lambda:json.loads(paths[0].read_text()))
    check(owner['state']=='recovery' and owner['target']==collection and owner['staging']==stage,'durable recovery ownership binds the original and retained copy')
    snapshot=Path(settings.upload_dir)/result['error_detail']['sidecar_snapshots']/'goldstandard'/(sid+'.json')
    check(await asyncio.to_thread(snapshot.read_bytes)==session_bytes,'pre-cutover evaluation snapshot is retained byte-identically')
    check(await asyncio.to_thread(lambda:gs.get_session(sid).get('stale')),'failed cutover marks its retained evaluation historical')
    await asyncio.to_thread(wc._sweep_staging_sync)
    check(await asyncio.to_thread(client.collections.exists,stage) and await asyncio.to_thread(paths[0].is_file),'actual startup sweep preserves retained recovery and its durable record')
    child="""import asyncio,json,sys
from services import weaviate_client as wc,goldstandard as gs,tuning
from main import app,lifespan
expected=json.load(sys.stdin)
async def proof():
    async with lifespan(app):
        assert wc.get_client().collections.exists(sys.argv[1])
        assert tuning._existing_records(sys.argv[1])==expected
        assert gs.get_session(sys.argv[2])['stale']
asyncio.run(proof())
print('PASS independent API lifespan restores recovery, exact records and historical session')
"""
    env={**os.environ,'UPLOAD_DIR':temp,'SOURCES_DIR':str(Path(temp)/'sources'),'EXPORTS_DIR':str(Path(temp)/'exports')}
    restarted=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',child,stage,sid],input=json.dumps(before),text=True,capture_output=True,env=env,timeout=30)
    if restarted.returncode:print(restarted.stderr,flush=True)
    check(restarted.returncode==0,'fresh API process restores owned recovery and exact records: '+restarted.stderr[-200:])
    deletion=await api.delete('/collections/'+stage[:1].lower()+stage[1:]+'?confirm=true')
    check(deletion.status_code==200 and deletion.json()['objects_deleted']==len(before),'explicit lowercase-alias DELETE removes the retained backend copy')
    check(not await asyncio.to_thread(paths[0].exists) and not await asyncio.to_thread(snapshot.parent.parent.exists),'explicit recovery deletion retires exactly its ownership journal and metadata snapshots')


async def additional_vectorizer_and_import_checks(api,client,name,temp,created,jobs,check):
    custom=name+'Custom'
    def create_custom():
        properties=[wc.Property(name=p.name,data_type=p.dataType,skip_vectorization=(p.name=='content')) for p in wc.COLLECTION_PROPERTIES]
        client.collections.create(name=custom,vectorizer_config=wc.Configure.Vectorizer.text2vec_ollama(api_endpoint='http://127.0.0.1:1',model=settings.embed_model,vectorize_collection_name=False),properties=properties)
        created.append(custom)
        client.collections.get(custom).data.insert(properties={'content':'Owned custom vectorizer input'},vector=[.125]*768)
    await asyncio.to_thread(create_custom)
    before=await asyncio.to_thread(tuning._existing_records,custom)
    request=await api.post('/tune/reindex',json={'collection':custom,'index_type':'flat','distance_metric':'dot'})
    assert request.status_code==202,request.text;identity=request.json()['job_id'];jobs.append((tuning,identity))
    refusal=await completed(api,'/tune/job/'+identity,expected_status='failed')
    check('vectorizer configuration' in refusal['error'] and refusal['chunks_written']==0,'actual custom property vectorization is refused before staging')
    check(await asyncio.to_thread(tuning._existing_records,custom)==before,'custom property refusal preserves real UUID/property/vector data')
    check(await asyncio.to_thread(lambda:not list(tuning.collection_recovery._root().glob('*.json'))),'custom property refusal creates no ownership or staging')
    # A distinct process registers actual import scratch, creates it, then exits
    # without Python finally. Startup ownership sweep must remove exactly it.
    parent=name+'ImportParent'
    code="from services import collection_recovery as r,weaviate_client as w; import os; o=r.begin("+repr(parent)+",'import',w.get_client()); w._create_collection_sync(o['staging'],'hnsw','cosine',{}); print(o['staging'],flush=True); os._exit(17)"
    env={**os.environ,'UPLOAD_DIR':temp,'SOURCES_DIR':str(Path(temp)/'sources')}
    result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code],env=env,text=True,capture_output=True,timeout=30)
    def records():return [json.loads(p.read_text()) for p in tuning.collection_recovery._root().glob('*.json') if json.loads(p.read_text()).get('target')==parent]
    ownership=await asyncio.to_thread(records)
    created.extend(o['staging'] for o in ownership)
    check(result.returncode==17 and len(ownership)==1 and ownership[0]['state']=='scratch','independent import scratch writer hard-exits with durable positive ownership')
    staging=ownership[0]['staging']
    check(await asyncio.to_thread(client.collections.exists,staging),'hard exit leaves the exact owned import staging collection')
    removed=await asyncio.to_thread(wc._sweep_staging_sync)
    check(staging in removed and not await asyncio.to_thread(client.collections.exists,staging),'startup removes the exact positively owned interrupted import scratch')
    check(await asyncio.to_thread(lambda:not records()),'startup completes and removes the owned import scratch journal')

async def collection_deletion_checks(api,client,name,temp,check):
    from services import sources,ingest_config,retrieval_config
    from routers import collections as collection_routes
    from weaviate.exceptions import UnexpectedStatusCodeError
    # Exercise both aliases on the Linux volume, with separate physical paths.
    for use_alias in (True,False):
        canonical=name+('AliasDelete' if use_alias else 'CanonicalDelete')
        alias=canonical[:1].lower()+canonical[1:]
        caller=alias if use_alias else canonical
        neighbor=canonical[:-1]+canonical[-1].upper()
        neighbor_supported=True
        for collection in (canonical,neighbor):
            try:
                await asyncio.to_thread(wc._create_collection_sync,collection,'hnsw','cosine',{})
            except UnexpectedStatusCodeError as exc:
                if collection != neighbor or exc.status_code != 422 or 'similar class' not in str(exc):
                    raise
                neighbor_supported=False
                print('NOTE backend rejects a case-only neighbor; verifying case-distinct sidecars and sessions on the Linux volume', flush=True)
                continue
            await asyncio.to_thread(client.collections.get(collection).data.insert,properties={'content':'Owned deletion fixture'},vector=[.125]*768)
        identities={spelling:'gs_'+uuid.uuid4().hex[:8] for spelling in (canonical,alias,neighbor)}
        for spelling,sid in identities.items():
            await asyncio.to_thread(sources.store,spelling,'owned.txt',b'Owned deletion original')
            await asyncio.to_thread(ingest_config.save,{'collection':spelling})
            await asyncio.to_thread(retrieval_config.save,{'collection':spelling})
            await asyncio.to_thread(gs.store_session,{'session_id':sid,'collection':spelling,'status':'completed','pairs_total':0,'pairs_completed':0,'pairs':[]})
        await asyncio.to_thread(collection_routes._save_registry, {canonical:'owned', alias:'owned', neighbor:'neighbor'})
        response=await api.delete('/collections/'+caller+'?confirm=true')
        registry=await asyncio.to_thread(collection_routes._load_registry)
        check(registry=={neighbor:'neighbor'},'registry removes both aliases and preserves case-distinct neighbor')
        check(response.status_code==200 and response.json()['objects_deleted']==1 and not await asyncio.to_thread(client.collections.exists,canonical),'HTTP deletion removes canonical backend collection via '+caller)
        for spelling in (canonical,alias):
            check(await asyncio.to_thread(lambda:not sources.collection_dir(spelling).exists() and not (Path(temp)/'ingest_configs'/(spelling+'.json')).exists() and not (Path(temp)/'retrieval_configs'/(spelling+'.json')).exists()),'deletion cleans exact source/ingest/retrieval spelling '+spelling)
            check(await asyncio.to_thread(lambda:gs.get_session(identities[spelling])['orphaned'] and json.loads(gs._session_path(identities[spelling]).read_text())['orphaned']),'deletion persists orphan status for '+spelling)
            check(gs.get_session(identities[spelling])['orphaned_reason']==f"collection '{canonical}' was deleted",'orphan reason names canonical collection for '+spelling)
        check(await asyncio.to_thread(lambda:(not neighbor_supported or client.collections.exists(neighbor)) and sources.collection_dir(neighbor).is_dir() and ingest_config.load(neighbor) is not None and retrieval_config.load(neighbor) is not None and not gs.get_session(identities[neighbor]).get('orphaned',False)),'deletion preserves case-distinct sidecars and current session')


async def caller_sidecar_and_legacy_tuning_checks(api,client,name,temp,created,check):
    await collection_deletion_checks(api,client,name,temp,check)
    before=await asyncio.to_thread(tuning._existing_records,name)
    child="from services import tuning,weaviate_client as w; import os; original=w._create_collection_sync; w._create_collection_sync=lambda *a,**k:(original(*a,**k),os._exit(17)); tuning._jobs['owned']={'status':'queued','chunks_written':0}; tuning._run('owned',"+repr(name)+",'reembed',{'index_type':'hnsw','distance_metric':'cosine'})"
    result=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',child],env={**os.environ,'UPLOAD_DIR':temp,'SOURCES_DIR':str(Path(temp)/'sources')},text=True,capture_output=True,timeout=30)
    def owners():return [json.loads(p.read_text()) for p in tuning.collection_recovery._root().glob('*.json') if json.loads(p.read_text()).get('target')==name]
    ownership=await asyncio.to_thread(owners);created.extend(o['staging'] for o in ownership)
    check(result.returncode==17 and len(ownership)==1 and ownership[0]['state']=='scratch','actual reembed worker hard-exits with positive staging ownership')
    stage=ownership[0]['staging'];check(await asyncio.to_thread(client.collections.exists,stage),'hard exit leaves exact positively owned legacy tuning staging')
    removed=await asyncio.to_thread(wc._sweep_staging_sync)
    check(stage in removed and not await asyncio.to_thread(client.collections.exists,stage) and await asyncio.to_thread(tuning._existing_records,name)==before and not await asyncio.to_thread(owners),'startup removes exact legacy tuning scratch while preserving original records')

async def main():
    from routers import collections as collection_routes
    token=uuid.uuid4().hex[:8]; name=owned_name(os.environ.get('RAG_TEST_PREFIX','Vfy49'),token)
    probe=name+'Probe'; sid='gs_'+token; job=None; jobs=[]; checks=0
    await bounded_poll_cases()
    for prefix in ('Vfy49','A','B'):
        if prefix:assert not owned_name(prefix,'owned').startswith(prefix)
    print('PASS parent cleanup namespace excludes verifier-owned names',flush=True)
    client=None;created=[]
    original_create=wc._create_collection_sync
    def record_create(collection,*args,**kwargs):
        original_create(collection,*args,**kwargs);created.append(collection)
    temporary=await asyncio.to_thread(tempfile.TemporaryDirectory,prefix='owned-reindex-')
    temp=temporary.name
    try:
        client=await asyncio.to_thread(wc.get_client)
        with patch.object(collection_routes,'_REGISTRY_FILE',Path(temp)/'collection_registry.json'), \
             patch.object(settings,'upload_dir',temp), patch.object(settings,'sources_dir',str(Path(temp)/'sources')), \
             patch.object(settings,'ollama_host','127.0.0.1'), patch.object(settings,'ollama_port',1), \
             patch.object(gs,'_sessions',{}),patch.object(wc.ingest_config,'_DIR',None),patch.object(wc.retrieval_config,'_DIR',None),patch.object(wc,'_create_collection_sync',side_effect=record_create):
            def check(condition,label):
                nonlocal checks
                assert condition,label; checks+=1; print('PASS '+label,flush=True)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned') as api:
                try:
                    await asyncio.to_thread(wc._create_collection_sync,probe,'hnsw','cosine',{})
                    try:
                        await asyncio.to_thread(client.collections.get(probe).data.insert,properties={'content':'Owned endpoint refusal probe'})
                    except Exception as exc:
                        message=str(exc).lower()
                        check('connect' in message and ('127.0.0.1:1' in message or 'connection refused' in message),'actual vectorization fails against the closed embedding endpoint')
                    else: raise AssertionError('Owned embedding endpoint unexpectedly served a vector')
                    check(await asyncio.to_thread(lambda:client.collections.get(probe).aggregate.over_all(total_count=True).total_count)==0,'failed embedding probe stores no object')
                    alias=name[:1].lower()+name[1:]
                    creation=await api.post('/collections',json={'name':alias,'index_type':'hnsw','distance_metric':'cosine'})
                    check(creation.status_code==201 and creation.json()['name']==name,'collection HTTP creation from a supported lowercase alias answers the stored name (#372)')
                    col=client.collections.get(name)
                    source=[]
                    for i in range(2):
                        identity=str(uuid.uuid4()); vector=[(i+1)/8.0]*768
                        props={'content':'Owned inert reindex '+str(i),'source_file':'owned-inert.txt','source_type':'txt','chunk_index':i,'chunk_strategy':'fixed','chunk_size':150,'chunk_overlap':0,'created_at':'2026-09-28T00:00:00Z'}
                        await asyncio.to_thread(col.data.insert,properties=props,uuid=identity,vector=vector)
                        source.append(identity)
                    before=await asyncio.to_thread(tuning._existing_records,name)
                    check({r['id'] for r in before}==set(source),'stored explicit vectors and original UUIDs are readable with embeddings unavailable')
                    session={'session_id':sid,'collection':name,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs_attempted':1,'pairs_failed':0,'pairs':[{'pair_id':'p_'+token,'question':'Owned question','answer':'Owned answer','ground_truth':'Owned truth','contexts':['Owned inert reindex'],'source_file':'owned-inert.txt','chunk_index':0,'status':'approved'}]}
                    await asyncio.to_thread(gs.store_session,session); session_bytes=await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes())
                    request=await api.post('/tune/reindex',json={'collection':alias,'index_type':'flat','distance_metric':'dot'})
                    check(request.status_code==202,'real reindex HTTP handler queues the job with closed embedding configuration'); job=request.json()['job_id'];jobs.append((tuning,job))
                    result=await completed(api,'/tune/job/'+job)
                    check(result['status']=='completed' and result['collection']==name and result['chunks_written']==len(before), 'job completes only after final backend verification: '+str(result))
                    after=await asyncio.to_thread(tuning._existing_records,name)
                    check({r['id']:r for r in after}=={r['id']:r for r in before},'UUIDs, every property and all stored vector values match exactly after reindex')
                    config=await asyncio.to_thread(wc._collection_config_sync,name)
                    check(config['index_type']=='flat' and config['distance_metric']=='dot','physical index and distance change to flat/dot')
                    check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes and not gs.get_session(sid).get('stale')),'retained evaluation identity/content/validity are unchanged')
                    check(any('verified unchanged' in note for note in result['notes']),'completion notes truthfully report verified identity and vector preservation')
                    with patch.object(settings,'embed_model','owned-incompatible-model'):
                        request=await api.post('/tune/reindex',json={'collection':name,'index_type':'hnsw','distance_metric':'cosine'})
                        assert request.status_code==202,request.text;job=request.json()['job_id'];jobs.append((tuning,job))
                        refused=await completed(api,'/tune/job/'+job,expected_status='failed')
                    check('vectorizer configuration' in refused['error'] and refused['chunks_written']==0,'foreign deployed embedding model is refused before replacement')
                    unchanged=await asyncio.to_thread(tuning._existing_records,name)
                    config_after_refusal=await asyncio.to_thread(wc._collection_config_sync,name)
                    check(unchanged==after and config_after_refusal==config,'refused model mismatch leaves real records and physical index unchanged')
                    check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes),'refused model mismatch preserves evaluation bytes')
                    check(await asyncio.to_thread(lambda:not list(tuning.collection_recovery._root().glob('*.json'))),'successful reindex removes its exact durable ownership; refusal creates none')
                    check(all(not item.startswith(os.environ.get('RAG_TEST_PREFIX','Vfy49')) for item in created),'all real verifier-created names remain outside the parent prefix sweep')
                    await additional_vectorizer_and_import_checks(api,client,name,temp,created,jobs,check)
                    await caller_sidecar_and_legacy_tuning_checks(api,client,name,temp,created,check)
                    await queued_ingest_checks(api,client,name,jobs,check)
                    await retained_cutover_checks(api,client,name,temp,record_create,jobs,check)
                    check(await asyncio.to_thread(lambda:gs._session_path(sid).read_bytes()==session_bytes),'failed secondary cutover leaves the unrelated primary evaluation unchanged')
                finally:
                    if jobs:
                        pending=await settle_owned_jobs(jobs)
                        if pending:
                            receipt=await asyncio.to_thread(preserve_receipt,temp,created,pending)
                            print(f'FAIL owned jobs still active: {pending}; preserving fixtures {created} and {receipt}',flush=True)
                            # Standalone verifier: preserve active fixtures and avoid executor join.
                            os._exit(2)
                    for owned in reversed(created):
                        if await asyncio.to_thread(client.collections.exists,owned): await asyncio.to_thread(client.collections.delete,owned)
                    check(not any([await asyncio.to_thread(client.collections.exists,item) for item in created]),'cleanup removes all exact recorded verifier-owned collections')
                    gs._sessions.pop(sid,None)
    finally:
        try:
            await asyncio.to_thread(temporary.cleanup)
        finally:
            if client is not None:await asyncio.to_thread(client.close)
    print(str(checks)+' real reindex checks passed',flush=True)

asyncio.run(main())
