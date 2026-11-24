"""Summarize explicitly reviewed evaluation values; unknown values are never counted as correct."""
import argparse,json
from pathlib import Path
parser=argparse.ArgumentParser();parser.add_argument('report',type=Path);args=parser.parse_args()
report=json.loads(args.report.read_text(encoding='utf-8'));rows=report['records'];modes=report.get('model_modes',[])
summary={}
for field in ['hit_at_5','prompt_document_hit','answer_correct','citation_supported','baseline_correct']:
    eligible=[row[field] for row in rows if isinstance(row.get(field),bool)]
    summary[field]={'reviewed_or_eligible':len(eligible),'total_records':len(rows),'ratio':round(sum(eligible)/len(eligible),4) if eligible else None}
unanswerable=[row for row in rows if row['category']=='unanswerable' and row.get('result')]
summary['unanswerable_insufficient_evidence']={'answered':len(unanswerable),'ratio':sum(bool(row['result']['insufficient_evidence']) for row in unanswerable)/len(unanswerable) if unanswerable else None}
summary['execution']={'expected':report.get('expected_records',len(rows)),'recorded':len(rows),
                      'succeeded':sum(bool(row.get('result')) for row in rows),
                      'failed':sum(bool(row.get('error')) for row in rows)}
if (rows and summary['execution']['expected']==len(rows)
        and summary['answer_correct']['reviewed_or_eligible']==summary['execution']['succeeded']
        and summary['execution']['succeeded']+summary['execution']['failed']==len(rows)):
    summary['correct_over_all']={'correct':sum(row.get('answer_correct') is True for row in rows),
                                 'total':len(rows)}
if unanswerable and all(isinstance(row.get('answer_correct'),bool) for row in unanswerable):
    summary['unanswerable_reviewed']={'correct':sum(row['answer_correct'] for row in unanswerable),
                                     'total':len(unanswerable)}
if 'mock' in modes:
    warning='Mock results are fixture checks, not real LLM quality.'
elif summary['answer_correct']['reviewed_or_eligible']<summary['execution']['succeeded']:
    warning='Some completed answers still require manual review.'
else:
    warning='Results describe this teaching fixture and configuration.'
print(json.dumps({'model_modes':modes,'warning':warning,'summary':summary},ensure_ascii=False,indent=2))
