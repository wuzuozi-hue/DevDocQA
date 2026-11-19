"""Measure admission latency separately from mock task completion; never label it LLM throughput."""
import argparse,asyncio,json,os,statistics,time,uuid
from pathlib import Path
import httpx
from smoke_test import ROOT,local_env

def percentile(values,p):
    if not values:return None
    values=sorted(values);return round(values[min(len(values)-1,int((len(values)-1)*p))],3)

async def run_benchmark(client,headers,document_ids,clients=(1,10,50)):
    phases=[]
    for count in clients:
        start=time.perf_counter()
        async def submit(index):
            tick=time.perf_counter()
            response=await client.post('/v1/questions',headers=headers,json={
              'question':f'RAII 如何在发生异常时释放资源？测试请求 {uuid.uuid4().hex}-{index}',
              'document_ids':document_ids})
            return response,(time.perf_counter()-tick)*1000
        admissions=await asyncio.gather(*(submit(i) for i in range(count)))
        completed=[]
        async def finish(response):
            if response.status_code!=202:return
            job_id=response.json()['job_id']
            for _ in range(1350):
                state=(await client.get('/v1/jobs/'+job_id,headers=headers)).json()
                if state['status']=='succeeded':
                    mode=state['result']['model_mode']
                    if mode!='mock':raise RuntimeError('This load test is restricted to mock model mode.')
                    completed.append('succeeded');return
                if state['status']=='failed':completed.append('failed');return
                await asyncio.sleep(.1)
            completed.append('poll_timeout')
        await asyncio.gather(*(finish(r) for r,_ in admissions))
        duration=time.perf_counter()-start
        statuses={str(status):sum(r.status_code==status for r,_ in admissions) for status in sorted({r.status_code for r,_ in admissions})}
        phases.append({'concurrent_clients':count,'submitted':count,'http_status_counts':statuses,
          'admission_p50_ms':percentile([n for _,n in admissions],.5),'admission_p95_ms':percentile([n for _,n in admissions],.95),
          'accepted':len(completed),'succeeded':completed.count('succeeded'),'failed':len(completed)-completed.count('succeeded'),
          'wall_seconds':round(duration,3),'successful_task_rate_per_second':round(completed.count('succeeded')/duration,3)})
    return {'model_mode':'mock','measurement':'single-run admission and task completion; not actual LLM throughput','phases':phases}

async def main(args):
    env=local_env();key=env.get('API_KEY');assert key,'Generate .env first'
    async with httpx.AsyncClient(base_url=env.get('DOCQA_URL','http://127.0.0.1:8080'),timeout=10,trust_env=False) as client:
        report=await run_benchmark(client,{'X-API-Key':key},args.document_ids)
    report['configured_limits']={name:env.get(name,'default') for name in ['AI_CONCURRENCY','QUEUE_CAPACITY','RATE_LIMIT_PER_MINUTE']}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Benchmark saved to '+str(args.output)+'. Review rejected requests and configured limits before interpreting throughput.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--document-ids',nargs='*',default=[])
    parser.add_argument('--output',type=Path,default=ROOT/'reports/generated/benchmark.json');asyncio.run(main(parser.parse_args()))
