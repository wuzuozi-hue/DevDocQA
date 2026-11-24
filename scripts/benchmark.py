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

async def run_mixed_benchmark(client, headers, rounds=5):
    async def upload(name, content):
        response = await client.post('/v1/documents', headers=headers,
                                     files={'file': (name, content.encode(), 'text/markdown')})
        response.raise_for_status()
        return response.json()

    async def get_job(job_id):
        response = await client.get(f'/v1/jobs/{job_id}', headers=headers)
        response.raise_for_status()
        return response.json()

    async def wait_job(job_id):
        deadline = time.monotonic() + 135
        while time.monotonic() < deadline:
            job = await get_job(job_id)
            if job['status'] in ('succeeded', 'failed'):
                return job
            await asyncio.sleep(.025)
        raise RuntimeError('mixed workload job did not complete')

    trial_id = uuid.uuid4().hex
    seed = await upload(f'raii-{trial_id}.md',
        f'# RAII\n\nRAII 在对象析构时释放锁和文件句柄。试验编号 {trial_id}。')
    if (await wait_job(seed['job_id']))['status'] != 'succeeded':
        raise RuntimeError('benchmark seed document did not become ready')

    trials = []
    all_question_ms = []
    for index in range(rounds):
        batch_start = time.perf_counter()
        uploads = []
        for n in range(6):
            uploads.append(await upload(f'mixed-{trial_id}-{index}-{n}.md',
                                        f'# 文档入库负载\n\n试验 {trial_id}，第 {index} 组的第 {n} 篇文档。'))
        upload_ids = [document['job_id'] for document in uploads]
        statuses = await asyncio.gather(*(get_job(job_id) for job_id in upload_ids))
        active_ingests = sum(job['status'] == 'running' for job in statuses)
        queued_ingests = sum(job['status'] == 'queued' for job in statuses)

        async def ask(question_index):
            tick = time.perf_counter()
            response = await client.post('/v1/questions', headers=headers, json={
                'question': f'RAII 何时释放文件句柄？测试 {trial_id}-{index}-{question_index}',
                'document_ids': [seed['document_id']],
            })
            if response.status_code != 202:
                return {'http_status': response.status_code, 'status': 'rejected'}
            job = await wait_job(response.json()['job_id'])
            if job['status'] == 'succeeded' and job['result']['model_mode'] != 'mock':
                raise RuntimeError('This load test is restricted to mock model mode.')
            return {'http_status': 202, 'status': job['status'], 'error': job['error'],
                    'completion_ms': round((time.perf_counter() - tick) * 1000, 3)}

        async def finish_ingests():
            jobs = await asyncio.gather(*(wait_job(job_id) for job_id in upload_ids))
            return {'succeeded': sum(job['status'] == 'succeeded' for job in jobs),
                    'seconds': round(time.perf_counter() - batch_start, 3)}

        ingest, questions = await asyncio.gather(finish_ingests(), asyncio.gather(ask(0), ask(1)))
        all_question_ms.extend(question['completion_ms'] for question in questions
                               if question['status'] == 'succeeded')
        trials.append({'active_ingests_at_submission': active_ingests,
                       'queued_ingests_at_submission': queued_ingests,
                       'ingest': ingest, 'questions': questions})

    upload_count = sum(trial['ingest']['succeeded'] for trial in trials)
    upload_seconds = sum(trial['ingest']['seconds'] for trial in trials)
    return {'model_mode': 'mock', 'rounds': rounds, 'uploads_per_round': 6, 'questions_per_round': 2,
            'successful_questions': len(all_question_ms),
            'question_completion_p50_ms': round(statistics.median(all_question_ms), 3) if all_question_ms else None,
            'question_completion_p95_ms': round(statistics.quantiles(all_question_ms, n=100, method='inclusive')[94], 3) if len(all_question_ms) > 1 else None,
            'successful_ingests': upload_count,
            'ingest_throughput_per_second': round(upload_count / upload_seconds, 3),
            'trials': trials}

async def main(args):
    env=local_env();key=env.get('API_KEY');assert key,'Generate .env first'
    async with httpx.AsyncClient(base_url=env.get('DOCQA_URL','http://127.0.0.1:8080'),timeout=10,trust_env=False) as client:
        headers={'X-API-Key':key}
        if args.mixed:
            report=await run_mixed_benchmark(client,headers)
        else:
            report=await run_benchmark(client,headers,args.document_ids)
    report['configured_limits']={name:env.get(name,'default') for name in
        ['AI_CONCURRENCY','QUEUE_CAPACITY','INGEST_CONCURRENCY','INGEST_QUEUE_CAPACITY','RATE_LIMIT_PER_MINUTE']}
    args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Benchmark saved to '+str(args.output)+'. Review rejected requests and configured limits before interpreting throughput.')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--document-ids',nargs='*',default=[])
    parser.add_argument('--mixed', action='store_true', help='Compare question latency during document ingestion in mock mode')
    parser.add_argument('--output',type=Path,default=ROOT/'reports/generated/benchmark.json');asyncio.run(main(parser.parse_args()))
