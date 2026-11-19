"""End-to-end real local model smoke, not a quality benchmark. Uses a migrated throwaway PG database."""
import argparse,asyncio,json,os,secrets,subprocess,sys,tempfile,threading,time
from pathlib import Path
import httpx
from fakeredis import TcpFakeServer

ROOT=Path(__file__).resolve().parents[1]

async def main(args):
    env=os.environ.copy();env.update(API_KEY=secrets.token_urlsafe(24),INTERNAL_KEY=secrets.token_urlsafe(24),PG_DSN=args.pg_dsn,
      MODEL_MODE='ollama',MODEL_SIGNATURE='ollama/qwen3:4b/bge-m3',EMBEDDING_SIGNATURE='ollama:bge-m3',
      OLLAMA_BASE_URL='http://127.0.0.1:11434',CHAT_MODEL='qwen3:4b',EMBED_MODEL='bge-m3',DOCQA_PORT='19081',WORKER_PORT='18002',
      REDIS_HOST='127.0.0.1',REDIS_PORT='16380',AI_SERVICE_URL='http://127.0.0.1:18002',AI_CONCURRENCY='1',QUEUE_CAPACITY='16',
      JOB_TIMEOUT_SECONDS='120',MODEL_TIMEOUT_SECONDS='110',RATE_LIMIT_PER_MINUTE='10',STATIC_DIR=str(ROOT/'web'))
    env['PATH']=str(Path(args.dll_dir).resolve())+os.pathsep+env['PATH'];flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
    server=TcpFakeServer(('127.0.0.1',16380),server_version=(7,4));server.daemon_threads=True
    threading.Thread(target=server.serve_forever,daemon=True).start();processes=[];streams=[]
    temporary=tempfile.TemporaryDirectory(prefix='docqa-ollama-');directory=Path(temporary.name);env['DATA_DIR']=str(directory/'documents');Path(env['DATA_DIR']).mkdir()
    try:
      for name,command,cwd in [('worker',[sys.executable,'-m','app'],ROOT/'worker'),('backend',[str(Path(args.backend).resolve())],ROOT)]:
        stream=(directory/(name+'.log')).open('ab');streams.append(stream)
        processes.append(subprocess.Popen(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,creationflags=flags))
      async with httpx.AsyncClient(base_url='http://127.0.0.1:19081',timeout=10,trust_env=False) as client:
        headers={'X-API-Key':env['API_KEY']}
        for _ in range(80):
          if any(p.poll() is not None for p in processes):raise RuntimeError('Service failed to start')
          try:
            if (await client.get('/ready')).status_code==200:break
          except httpx.HTTPError:pass
          await asyncio.sleep(.1)
        else:raise RuntimeError('Services not ready')
        async def poll(job_id):
          for _ in range(135):
            response=await client.get('/v1/jobs/'+job_id,headers=headers);response.raise_for_status();job=response.json()
            if job['status']=='succeeded':return job['result']
            if job['status']=='failed':raise RuntimeError('Task failed: '+job['error'])
            await asyncio.sleep(1)
          raise RuntimeError('Task polling timeout')
        content='# RAII\n\nRAII把资源生命周期绑定到对象生命周期。构造函数获取资源，析构函数释放资源。栈上对象在正常离开作用域和异常栈展开时都会调用析构函数，因此资源会自动清理。'
        start=time.perf_counter();response=await client.post('/v1/documents',headers=headers,files={'file':('real-raii.md',content.encode())});response.raise_for_status();upload=response.json();ingest=await poll(upload['job_id']);ingestion_seconds=time.perf_counter()-start
        start=time.perf_counter();response=await client.post('/v1/questions',headers=headers,json={'question':'根据文档，RAII如何保证发生异常时释放资源？','document_ids':[upload['document_id']]});response.raise_for_status();result=await poll(response.json()['job_id']);answer_seconds=time.perf_counter()-start
        assert result['model_mode']=='ollama' and result['citations'] and not result['insufficient_evidence'],result
        assert all(c['document_id']==upload['document_id'] for c in result['citations'])
        report={'status':'passed','model_mode':'ollama','generation_model':'qwen3:4b','embedding_model':'bge-m3',
          'database':'real PostgreSQL+pgvector','redis':'fakeredis TCP test double','case':'one short document and one answerable question',
          'quality_acceptance':'Not evaluated: this smoke does not establish Hit@5/answer accuracy targets.',
          'ingestion_seconds':round(ingestion_seconds,3),'answer_seconds':round(answer_seconds,3),'result':result}
        args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))
    except Exception:
      for name in ('backend','worker'):
        path=directory/(name+'.log')
        if path.exists():print(name+': '+path.read_text(encoding='utf-8',errors='replace')[-2000:])
      raise
    finally:
      for process in reversed(processes):
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
      for stream in streams:stream.close()
      server.shutdown();server.server_close();temporary.cleanup()

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--backend',required=True);parser.add_argument('--dll-dir',required=True);parser.add_argument('--pg-dsn',required=True)
    parser.add_argument('--report',type=Path,default=ROOT/'reports/ollama-smoke.json');asyncio.run(main(parser.parse_args()))
