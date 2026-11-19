"""Exercise the real public C++ API. Run after compose --wait, using local demo credentials."""
import asyncio, json, os
from pathlib import Path
import httpx

ROOT=Path(__file__).resolve().parents[1]
def local_env():
    values={}
    path=ROOT/'.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                k,v=line.split('=',1);values[k.strip()]=v.strip()
    values.update(os.environ);return values

async def run():
    env=local_env();key=env.get('API_KEY');assert key,'Generate .env first'
    async with httpx.AsyncClient(base_url=env.get('DOCQA_URL','http://127.0.0.1:8080'),timeout=10,trust_env=False) as client:
        assert (await client.get('/health')).status_code==200
        assert (await client.get('/v1/documents')).status_code==401
        headers={'X-API-Key':key}
        empty=await client.post('/v1/questions',headers=headers,json={'question':''});assert empty.status_code==400,empty.text
        source='# RAII\n\nRAII 将资源的生命周期绑定到对象的生命周期。对象析构时释放资源，即使发生异常也能清理资源。\n'
        response=await client.post('/v1/documents',headers=headers,files={'file':('smoke-raii.md',source.encode(),'text/markdown')})
        assert response.status_code in {200,201,202},response.text
        document=response.json()
        async def poll(job_id):
            for _ in range(135):
                response=await client.get('/v1/jobs/'+job_id,headers=headers);assert response.status_code==200,response.text
                body=response.json()
                if body['status']=='succeeded':return body['result']
                assert body['status']!='failed',body.get('error','task failed')
                await asyncio.sleep(1)
            raise AssertionError('Task did not finish in 135s')
        if document.get('job_id'):await poll(document['job_id'])
        ready=await client.get('/v1/documents',headers=headers);assert ready.status_code==200
        response=await client.post('/v1/questions',headers=headers,json={'question':'RAII 如何在异常时释放资源？','document_ids':[document['document_id']]})
        assert response.status_code==202,response.text
        result=await poll(response.json()['job_id'])
        if isinstance(result,str):result=json.loads(result)
        assert result['answer'] and result['citations'],result
        assert all(c['document_id']==document['document_id'] for c in result['citations'])
        missing=await client.get('/v1/jobs/does-not-exist',headers=headers);assert missing.status_code==404
        print('Public API smoke test passed: health/auth/validation/ingestion/answer/citations/not-found.')
if __name__=='__main__':asyncio.run(run())
