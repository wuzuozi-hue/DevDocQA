"""Switch local model configuration coherently, without touching stored document data."""
import argparse
from pathlib import Path

parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['mock','ollama']);args=parser.parse_args()
path=Path(__file__).resolve().parents[1]/'.env'
if not path.exists():raise SystemExit('Generate .env with scripts/init_env.py first.')
lines=path.read_text(encoding='utf-8').splitlines()
values={}
for line in lines:
    if '=' in line and not line.startswith('#'):
        k,v=line.split('=',1);values[k]=v
updates={'MODEL_MODE':args.mode,'EMBEDDING_SIGNATURE':'mock:featurehash-v1' if args.mode=='mock' else 'ollama:'+values.get('EMBED_MODEL','bge-m3')}
found=set();new=[]
for line in lines:
    k=line.split('=',1)[0]
    if k in updates:line=k+'='+updates[k];found.add(k)
    new.append(line)
for k,v in updates.items():
    if k not in found:new.append(k+'='+v)
path.write_text('\n'.join(new)+'\n',encoding='utf-8')
print('Mode set to '+args.mode+'. Restart services, then reupload existing documents to reindex. Credentials kept unchanged.')
