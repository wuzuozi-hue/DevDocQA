"""Summarize explicitly reviewed evaluation values; unknown values are never counted as correct."""
import argparse,json
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('report',type=Path);args=parser.parse_args()
report=json.loads(args.report.read_text(encoding='utf-8'));rows=report['records'];modes=report.get('model_modes',[])
summary={}
for field in ['hit_at_5','answer_correct','citation_supported','baseline_correct']:
    eligible=[row[field] for row in rows if isinstance(row.get(field),bool)]
    summary[field]={'reviewed_or_eligible':len(eligible),'total_records':len(rows),'ratio':round(sum(eligible)/len(eligible),4) if eligible else None}
unanswerable=[row for row in rows if row['category']=='unanswerable' and row.get('result')]
summary['unanswerable_insufficient_evidence']={'answered':len(unanswerable),'ratio':sum(bool(row['result']['insufficient_evidence']) for row in unanswerable)/len(unanswerable) if unanswerable else None}
print(json.dumps({'model_modes':modes,'warning':'Mock results are fixture checks, not real LLM quality.' if 'mock' in modes else 'Manual review must be completed before claiming model accuracy.','summary':summary},ensure_ascii=False,indent=2))
