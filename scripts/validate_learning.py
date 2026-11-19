"""Check evaluation split integrity and reference document existence, without scoring model quality."""
from pathlib import Path
from collections import Counter
import json

root = Path(__file__).resolve().parents[1]
docs = {x.name for x in (root/'learning/corpus').glob('*.md')}
rows = [json.loads(x) for x in (root/'learning/eval/questions.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
assert len(docs)==30, f'Expected 30 docs, got {len(docs)}'
assert len(rows)==60 and len({r['id'] for r in rows})==60
assert Counter(r['split'] for r in rows)=={'dev':20,'test':40}
categories = Counter(r['category'] for r in rows)
assert sorted(categories.values())==[10,10,40], categories
for row in rows:
    assert row['question'] and row['reference_answer']
    assert set(row['expected_document_names']) <= docs, row['id']
    assert row['split'] in {'dev','test'}
print('Learning fixtures valid: 30 documents, 60 questions, dev20/test40. No model scores inferred.')
