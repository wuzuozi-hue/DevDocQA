"""Run the already prepared Windows development demo. Redis is explicitly a test double.

The portable dependencies stay in the workspace work/ directory. For independent
deployment use Compose with actual Redis; this launcher is for this learning machine.
"""
import argparse,os,subprocess,sys,threading,time
from pathlib import Path
import httpx,psycopg
from fakeredis import TcpFakeServer
from smoke_test import ROOT,local_env

def main(args):
    native=args.native_root.resolve();prefix=native/'msys/ucrt64';bin_dir=prefix/'bin'
    candidates=[native/'backend-clang/devdocqa.exe',native/'backend-build/devdocqa.exe']
    backend=next((p for p in candidates if p.exists()),None)
    if backend is None:raise SystemExit('Portable backend not found. Build the backend or use Compose.')
    env=os.environ.copy();env.update(local_env());env['PATH']=str(bin_dir)+os.pathsep+env['PATH']
    if not env.get('API_KEY') or not env.get('INTERNAL_KEY'):raise SystemExit('Run scripts/init_env.py first.')
    flags=getattr(subprocess,'CREATE_NO_WINDOW',0);processes=[];streams=[];owned_pg=False;redis_server=None
    data=ROOT/'data/native-demo';documents=data/'documents';documents.mkdir(parents=True,exist_ok=True)
    pgdata=native/'pgdata';admin='host=127.0.0.1 port=55432 user=docqa dbname=postgres'
    env.update(PG_DSN='host=127.0.0.1 port=55432 user=docqa dbname=devdocqa_demo',REDIS_HOST='127.0.0.1',REDIS_PORT='16381',
        DOCQA_PORT=str(args.port),DOCQA_BIND='127.0.0.1',WORKER_PORT='18003',AI_SERVICE_URL='http://127.0.0.1:18003',
        DATA_DIR=str(documents),STATIC_DIR=str(ROOT/'web'),OLLAMA_BASE_URL='http://127.0.0.1:11434')
    if args.mode:env['MODEL_MODE']=args.mode
    mode=env.get('MODEL_MODE','mock');env['EMBEDDING_SIGNATURE']='mock:featurehash-v1' if mode=='mock' else 'ollama:'+env.get('EMBED_MODEL','bge-m3')
    env['MODEL_SIGNATURE']=mode+'/'+env.get('CHAT_MODEL','qwen3:4b')+'/'+env.get('EMBED_MODEL','bge-m3')
    def launch(name,command,cwd):
        stream=(data/(name+'.log')).open('ab');streams.append(stream)
        process=subprocess.Popen(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,creationflags=flags)
        processes.append(process);return process
    try:
      try:
        connection=psycopg.connect(admin,connect_timeout=2,autocommit=True)
      except psycopg.OperationalError:
        if not (pgdata/'PG_VERSION').exists():raise SystemExit('Prepared PostgreSQL cluster missing; use Compose or initialize a development cluster.')
        subprocess.run([str(bin_dir/'pg_ctl.exe'),'-D',str(pgdata),'-l',str(data/'postgres.log'),'-o','-h 127.0.0.1 -p 55432','-w','start'],
                       env=env,creationflags=flags,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=30)
        owned_pg=True;connection=psycopg.connect(admin,connect_timeout=5,autocommit=True)
      with connection:
        if not connection.execute('SELECT 1 FROM pg_database WHERE datname=%s',('devdocqa_demo',)).fetchone():connection.execute('CREATE DATABASE devdocqa_demo')
      with psycopg.connect(env['PG_DSN'],autocommit=True) as connection:
        connection.execute((ROOT/'db/001_initial.sql').read_text(encoding='utf-8'))
        connection.execute((ROOT/'db/002_embedding_signature.sql').read_text(encoding='utf-8'))
      if mode=='ollama':
        try:httpx.get('http://127.0.0.1:11434/api/version',timeout=2,trust_env=False).raise_for_status()
        except httpx.HTTPError:
          ollama_root=ROOT.parents[1]/'work/ollama-setup'
          choices=list(ollama_root.rglob('ollama.exe'))
          if len(choices)!=1:raise SystemExit('Local Ollama is not running. Start Ollama first, or use --mode mock.')
          env.update(OLLAMA_HOST='127.0.0.1:11434',OLLAMA_MODELS=str(ollama_root/'models'),USERPROFILE=str(ollama_root/'profile'),OLLAMA_NO_CLOUD='1')
          launch('ollama',[str(choices[0]),'serve'],choices[0].parent)
          for _ in range(40):
            try:
              httpx.get('http://127.0.0.1:11434/api/version',timeout=1,trust_env=False).raise_for_status();break
            except httpx.HTTPError:time.sleep(.25)
      redis_server=TcpFakeServer(('127.0.0.1',16381),server_version=(7,4));redis_server.daemon_threads=True
      threading.Thread(target=redis_server.serve_forever,daemon=True).start()
      launch('worker',[sys.executable,'-m','app'],ROOT/'worker');launch('backend',[str(backend)],ROOT)
      for _ in range(80):
        if any(p.poll() is not None for p in processes):raise RuntimeError('A service exited; inspect data/native-demo/*.log.')
        try:
          httpx.get(f'http://127.0.0.1:{args.port}/ready',timeout=2,trust_env=False).raise_for_status();break
        except httpx.HTTPError:time.sleep(.1)
      else:raise RuntimeError('Demo dependencies did not become ready.')
      print(f'DevDocQA ready: http://127.0.0.1:{args.port} | model={mode}',flush=True)
      print('Development preview: Redis is a TCP test double. API Key remains in local .env. Ctrl+C stops owned services.',flush=True)
      while all(p.poll() is None for p in processes):time.sleep(1)
    except KeyboardInterrupt:pass
    finally:
      for process in reversed(processes):
        if process.poll() is None:process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
      for stream in streams:stream.close()
      if redis_server is not None:redis_server.shutdown();redis_server.server_close()
      if owned_pg:
        subprocess.run([str(bin_dir/'pg_ctl.exe'),'-D',str(pgdata),'-m','fast','stop'],env=env,creationflags=flags,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--native-root',type=Path,default=ROOT.parents[1]/'work/devdocqa-build')
    parser.add_argument('--mode',choices=['mock','ollama']);parser.add_argument('--port',type=int,default=18880);main(parser.parse_args())
