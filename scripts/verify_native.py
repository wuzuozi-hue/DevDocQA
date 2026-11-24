"""Black-box tests with a real C++ server and PostgreSQL; Redis is a labelled TCP test double.

Requires a prebuilt backend, its DLL directory, and a disposable migrated PostgreSQL DB.
No processes or ports are left running. This is not the production Docker acceptance run.
"""
import argparse, asyncio, hashlib, json, os, secrets, subprocess, sys, tempfile, threading, time
from pathlib import Path
import httpx
import psycopg
from fakeredis import TcpFakeServer

ROOT=Path(__file__).resolve().parents[1]

async def check(args):
    checks=[]
    env=os.environ.copy();env.update(API_KEY=secrets.token_urlsafe(24),INTERNAL_KEY=secrets.token_urlsafe(24),PG_DSN=args.pg_dsn,
        MODEL_MODE='mock',MODEL_SIGNATURE='mock/qwen3:4b/bge-m3',EMBEDDING_SIGNATURE='mock:featurehash-v1',
        DOCQA_PORT=str(args.port),WORKER_PORT=str(args.worker_port),REDIS_HOST='127.0.0.1',REDIS_PORT=str(args.redis_port),
        AI_SERVICE_URL=f'http://127.0.0.1:{args.worker_port}',AI_CONCURRENCY='1',QUEUE_CAPACITY='4',
        JOB_TIMEOUT_SECONDS='120',RATE_LIMIT_PER_MINUTE='6000',MOCK_DELAY_SECONDS='0.3',
        STATIC_DIR=str(ROOT/'web'))
    env['PATH']=str(Path(args.dll_dir).resolve())+os.pathsep+env['PATH']
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    redis_server=TcpFakeServer(('127.0.0.1',args.redis_port),server_version=(7,4));redis_server.daemon_threads=True
    redis_thread=threading.Thread(target=redis_server.serve_forever,daemon=True);redis_thread.start()
    processes=[];streams=[]
    def start_backend(extra=None):
        config=env.copy();config.update(extra or {})
        log=tempdir/'backend.log';stream=log.open('ab');streams.append(stream)
        process=subprocess.Popen([str(Path(args.backend).resolve())],cwd=ROOT,env=config,stdout=stream,stderr=subprocess.STDOUT,creationflags=flags)
        processes.append(process);return process
    def stop(process):
        if process.poll() is None:
            process.terminate()
            try:process.wait(timeout=8)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=8)
    async def wait_ready(client):
        last='no response'
        for _ in range(80):
            if processes[-1].poll() is not None:
                raise AssertionError('Backend exited during startup: '+str(processes[-1].returncode))
            try:
                response=await client.get('/ready',timeout=3)
                if response.status_code==200:return
                last=str(response.status_code)+' '+response.text[:300]
            except httpx.HTTPError:pass
            await asyncio.sleep(.1)
        raise AssertionError('C++ readiness did not succeed: '+last)
    temporary=tempfile.TemporaryDirectory(prefix='docqa-native-')
    tempdir=Path(temporary.name)
    try:
      if True:
        env['DATA_DIR']=str(tempdir/'documents');Path(env['DATA_DIR']).mkdir()
        stream=(tempdir/'worker.log').open('ab');streams.append(stream)
        worker=subprocess.Popen([sys.executable,'-m','app'],cwd=ROOT/'worker',env=env,stdout=stream,stderr=subprocess.STDOUT,creationflags=flags);processes.append(worker)
        backend=start_backend()
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{args.port}',timeout=10,trust_env=False) as client:
          await wait_ready(client);headers={'X-API-Key':env['API_KEY']}
          async def call(method,path,**kwargs):return await client.request(method,path,headers=headers,**kwargs)
          async def poll(job_id,expected='succeeded'):
            for _ in range(160):
              response=await call('GET','/v1/jobs/'+job_id);assert response.status_code==200,response.text
              body=response.json()
              if body['status'] in ('succeeded','failed'):
                assert body['status']==expected,body;return body
              await asyncio.sleep(.1)
            raise AssertionError('job did not complete')
          health=await client.get('/health');assert health.status_code==200
          counts=health.json()
          assert set(counts['scheduler'])=={'question','ingest'}
          assert counts['active_jobs']==sum(queue['active'] for queue in counts['scheduler'].values())
          assert counts['queued_jobs']==sum(queue['queued'] for queue in counts['scheduler'].values())
          assert (await client.get('/v1/documents')).status_code==401
          assert (await call('GET','/v1/jobs/not-found')).status_code==404
          assert (await call('POST','/v1/questions',json={'question':''})).status_code==400
          assert (await call('POST','/v1/questions',json={'question':'RAII'})).status_code==409
          for name,content,status in [('empty.md',b'',400),('bad.pdf',b'PDF',415),('bad.md',b'\xff\xfe',400),('large.md',b'x'*(2*1024*1024+1),413)]:
            response=await call('POST','/v1/documents',files={'file':(name,content)});assert response.status_code==status,(name,response.status_code,response.text[:100])
          checks.append('health,auth,validation,empty-library,not-found')
          content='# RAII\n\nRAII 将资源的生命周期绑定到对象的生命周期。对象析构时释放资源，即使发生异常也能清理资源。'
          response=await call('POST','/v1/documents',files={'file':('raii.md',content.encode())});assert response.status_code==202,response.text
          upload=response.json();await poll(upload['job_id'])
          duplicate=await call('POST','/v1/documents',files={'file':('raii.md',content.encode())});assert duplicate.status_code==202,duplicate.text
          assert duplicate.json()['document_id']==upload['document_id'] and duplicate.json()['duplicate']
          question={'question':'RAII 如何在发生异常时清理资源？','document_ids':[upload['document_id']]}
          response=await call('POST','/v1/questions',json=question);assert response.status_code==202,response.text
          answer=(await poll(response.json()['job_id']))['result'];assert answer['model_mode']=='mock' and answer['citations'],answer
          assert all(c['document_id']==upload['document_id'] for c in answer['citations'])
          checks.append('ingestion,duplicate,scoped-answer,valid-citations')
          cached=await call('POST','/v1/questions',json=question);assert cached.status_code==202 and cached.json()['cached'],cached.text
          changed=await call('POST','/v1/documents',files={'file':('http.md','HTTP 是应用层协议。请求包含方法、路径和头部。'.encode())});await poll(changed.json()['job_id'])
          uncached=await call('POST','/v1/questions',json=question);assert not uncached.json()['cached'];await poll(uncached.json()['job_id'])
          checks.append('exact-cache,collection-version-invalidation')
          responses=await asyncio.gather(*(call('POST','/v1/questions',json={**question,'question':question['question']+str(i)}) for i in range(20)))
          accepted=[r.json()['job_id'] for r in responses if r.status_code==202]
          rejected=[r for r in responses if r.status_code==503]
          assert accepted and rejected and all(r.status_code in (202,503) for r in responses),[r.status_code for r in responses]
          await asyncio.gather(*(poll(j) for j in accepted));checks.append('parallel-admission,bounded-queue')
          redis_server.fake_server.connected=False
          unavailable=await call('POST','/v1/questions',json=question);assert unavailable.status_code==503,unavailable.text
          redis_server.fake_server.connected=True;checks.append('redis-unavailable-fail-closed')
          from benchmark import run_benchmark
          benchmark=await run_benchmark(client,headers,[upload['document_id']])
          benchmark['environment']='native Windows C++ binary; real PostgreSQL/pgvector; Redis TCP test double'
          benchmark['configured_limits']={'AI_CONCURRENCY':1,'QUEUE_CAPACITY':4,'RATE_LIMIT_PER_MINUTE':6000,'MOCK_DELAY_SECONDS':.3}
          benchmark_path=args.report.parent/'native-benchmark.json';benchmark_path.parent.mkdir(parents=True,exist_ok=True)
          benchmark_path.write_text(json.dumps(benchmark,ensure_ascii=False,indent=2),encoding='utf-8');checks.append('measured-mock-load-1-10-50-clients')
          stop(backend);backend=start_backend({'API_KEY':env['API_KEY']+'new','RATE_LIMIT_PER_MINUTE':'2'});headers['X-API-Key']=env['API_KEY']+'new';await wait_ready(client)
          for i in range(2):
            response=await call('POST','/v1/questions',json={**question,'question':question['question']+'rate'+str(i)});assert response.status_code==202,response.text;await poll(response.json()['job_id'])
          assert (await call('POST','/v1/questions',json=question)).status_code==429;checks.append('fixed-window-rate-limit')
          stop(backend);backend=start_backend({'JOB_TIMEOUT_SECONDS':'1','RATE_LIMIT_PER_MINUTE':'6000'});headers['X-API-Key']=env['API_KEY'];await wait_ready(client)
          # One active and four waiting tasks with a fixed model delay force queued deadlines.
          responses=await asyncio.gather(*(call('POST','/v1/questions',json={**question,'question':question['question']+'deadline'+str(i)}) for i in range(5)))
          states=[]
          for response in responses:
            if response.status_code==202:
              job_id=response.json()['job_id']
              for _ in range(50):
                body=(await call('GET','/v1/jobs/'+job_id)).json()
                if body['status'] in ('failed','succeeded'):states.append(body['status']);break
                await asyncio.sleep(.1)
          assert 'failed' in states,states;checks.append('total-deadline-includes-queue')
          stop(backend)
          # Seed the disposable DB with both queued kinds before booting, so both
          # tasks must use the recovery path instead of the HTTP submission path.
          suffix=secrets.token_hex(8)
          recovery_doc=f'recovery_{suffix}'
          recovery_question=f'question_{suffix}'
          recovery_ingest=f'ingest_{suffix}'
          recovery_file=tempdir/'documents'/f'{recovery_doc}.md'
          recovery_file.write_bytes('# 重启恢复\n\n这段原文用于验证重新入库。'.encode())
          digest=hashlib.sha256(recovery_file.read_bytes()).hexdigest()
          with psycopg.connect(args.pg_dsn) as connection:
            connection.execute("INSERT INTO documents(id,name,sha256,status) VALUES(%s,%s,%s,'queued')",
                               (recovery_doc, recovery_file.name, digest))
            connection.execute("INSERT INTO jobs(id,kind,status,payload) VALUES(%s,'ingest','queued',%s::jsonb)",
                               (recovery_ingest, json.dumps({'document_id':recovery_doc,'path':str(recovery_file),
                                                            'name':recovery_file.name,'sha256':digest})))
            connection.execute("INSERT INTO jobs(id,kind,status,payload) VALUES(%s,'question','queued',%s::jsonb)",
                               (recovery_question, json.dumps(question)))
          backend=start_backend();await wait_ready(client)
          recovered_question,recovered_ingest=await asyncio.gather(poll(recovery_question),poll(recovery_ingest))
          assert recovered_question['result']['model_mode']=='mock'
          assert recovered_ingest['result']['chunks_count']>0
          recovery=await call('GET','/v1/documents');assert recovery.status_code==200
          assert any(row['id']==recovery_doc and row['status']=='ready' for row in recovery.json()['documents'])
          checks.append('backend-restart,both-queues-recovered,persistent-document-state')
        report={'cpp_backend':'real C++17 Drogon server','database':'real PostgreSQL with pgvector',
          'redis':'fakeredis TCP server with Lua (test double, not real Redis)','model':'mock, fixed generation delay 0.3s',
          'checks':checks,'status':'passed','queue_capacity_test_override':4}
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
    except Exception:
        for name in ('backend.log','worker.log'):
            path=tempdir/name
            if path.exists():print(name+':\n'+path.read_text(encoding='utf-8',errors='replace')[-5000:])
        raise
    finally:
        for process in reversed(processes):stop(process)
        for stream in streams:stream.close()
        redis_server.shutdown();redis_server.server_close()
        temporary.cleanup()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backend',required=True);parser.add_argument('--dll-dir',required=True)
    parser.add_argument('--pg-dsn',required=True);parser.add_argument('--port',type=int,default=19080);parser.add_argument('--worker-port',type=int,default=18001)
    parser.add_argument('--redis-port',type=int,default=16379);parser.add_argument('--report',type=Path,default=ROOT/'reports/native-integration.json');args=parser.parse_args()
    asyncio.run(check(args))
