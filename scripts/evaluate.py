"""Record retrieval hits and model outputs. Correctness/citation support require human scoring."""
import argparse
import asyncio
import json
import time
from pathlib import Path
import httpx
from smoke_test import ROOT,local_env

async def evaluate(args):
    env=local_env();key=env.get('API_KEY');assert key,'Generate .env first'
    rows=[json.loads(line) for line in (ROOT/'learning/eval/questions.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    selected=[row for row in rows if row['split']==args.split and (not args.case or row['id'] in args.case)]
    missing=set(args.case or [])-{row['id'] for row in selected}
    if missing:
        raise ValueError(f"Cases not found in {args.split}: {', '.join(sorted(missing))}")
    strategies=('rag','agent') if args.strategy=='both' else (args.strategy,)
    trials=[(row,strategy) for row in selected for strategy in strategies]
    headers={'X-API-Key':key};records=[]
    document_ids=[]
    args.output.parent.mkdir(parents=True,exist_ok=True)

    def checkpoint():
        modes=sorted({r['result']['model_mode'] for r in records if r['result']})
        report={'split':args.split,'strategies':strategies,'model_modes':modes,'expected_records':len(trials),
                'fixture':'original educational corpus; not a public benchmark',
                'automatic_scoring':'document coverage at retrieval and in the model prompt; answer/citation support require manual review',
                'records':records}
        temporary=args.output.with_name(args.output.name+'.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(args.output)

    async with httpx.AsyncClient(base_url=env.get('DOCQA_URL','http://127.0.0.1:8080'),timeout=10,trust_env=False) as client:
        async def baseline(question):
            if not args.baseline:return None
            async with httpx.AsyncClient(timeout=110,trust_env=False) as model:
                response=await model.post(args.ollama_url+'/api/chat',json={
                  'model':env.get('CHAT_MODEL','qwen3:4b'),'stream':False,'think':False,
                  'options':{'temperature':0,'num_ctx':8192,'num_predict':768},
                  'messages':[{'role':'system','content':'用中文回答开发技术问题；不知道时明确说明。'},
                              {'role':'user','content':question}]})
                response.raise_for_status();return response.json()['message']['content']
        async def poll(job_id):
            for _ in range(135):
                response=await client.get('/v1/jobs/'+job_id,headers=headers);response.raise_for_status();body=response.json()
                if body['status']=='succeeded':return body['result']
                if body['status']=='failed':raise RuntimeError(body['error'])
                await asyncio.sleep(1)
            raise RuntimeError('task polling exceeded 135s')
        if args.ingest:
            for path in sorted((ROOT/'learning/corpus').glob('*.md')):
                response=await client.post('/v1/documents',headers=headers,files={'file':(path.name,path.read_bytes(),'text/markdown')})
                response.raise_for_status()
                upload=response.json()
                if upload.get('job_id'):
                    await poll(upload['job_id'])
                document_ids.append(upload['document_id'])
        for index,(row,strategy) in enumerate(trials):
            start=time.perf_counter();result=None;error=None;cached=None
            for attempt in range(13):
                try:
                    response=await client.post('/v1/questions',headers=headers,
                                               json={'question':row['question'],'document_ids':document_ids,
                                                     'strategy':strategy})
                except httpx.HTTPError as failure:
                    error=type(failure).__name__
                    break
                if response.status_code==429:
                    await asyncio.sleep(5);continue
                if response.status_code!=202:error=f'HTTP {response.status_code}';break
                submission=response.json()
                cached=submission['cached']
                try:result=await poll(submission['job_id'])
                except RuntimeError as failure:error=str(failure)
                except httpx.HTTPError as failure:error=type(failure).__name__
                break
            if result is None and error is None:error='rate limit did not reset within 65s'
            elapsed=round(time.perf_counter()-start,3)
            retrieval=(result or {}).get('retrieval',[])
            retrieved={s['document_name'] for s in retrieval}
            trace=(result or {}).get('evidence_trace')
            in_prompt={s['document_name'] for s in retrieval
                       if trace and s['source_id'] in trace['included_source_ids']}
            expected=set(row['expected_document_names'])
            baseline_answer=None
            baseline_error=None
            if args.baseline and result and result['model_mode']=='ollama':
                try:baseline_answer=await baseline(row['question'])
                except httpx.HTTPError as failure:baseline_error=type(failure).__name__
            records.append({**row,'strategy':strategy,'cached':cached,'result':result,'baseline_answer':baseline_answer,
              'baseline_error':baseline_error,'baseline_correct':None,'error':error,'elapsed_seconds':elapsed,
              'hit_at_5':expected.issubset(retrieved) if expected and not error and strategy=='rag' else None,
              'selected_document_hit':expected.issubset(retrieved) if expected and not error and strategy=='agent' else None,
              'prompt_document_hit':expected.issubset(in_prompt) if expected and trace else None,
              'answer_correct':None,'citation_supported':None,'review_notes':''})
            checkpoint()
            print(f'{index+1}/{len(trials)} {row["id"]} {strategy} '+('recorded' if not error else error),flush=True)
    print('Saved '+str(args.output)+'. Mock answers cannot establish actual model accuracy.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--split',choices=['dev','test'],default='dev');parser.add_argument('--ingest',action='store_true')
    parser.add_argument('--strategy',choices=['rag','agent','both'],default='rag')
    parser.add_argument('--case',action='append',help='Run only the specified case ID (repeat to select more cases)')
    parser.add_argument('--baseline',action='store_true',help='Also ask the same local Ollama model without retrieved evidence')
    parser.add_argument('--ollama-url',default='http://127.0.0.1:11434')
    parser.add_argument('--output',type=Path,default=ROOT/'reports/generated/evaluation.json');asyncio.run(evaluate(parser.parse_args()))
