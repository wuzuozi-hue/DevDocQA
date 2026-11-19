"""Create local-only app credentials, without printing secrets or overwriting an existing file."""
from pathlib import Path
import secrets

root = Path(__file__).resolve().parents[1]
target = root / '.env'
if target.exists():
    print('.env exists; kept unchanged.')
else:
    text = (root / '.env.example').read_text(encoding='utf-8')
    replacements = {
        'replace-with-generated-local-demo-key': secrets.token_urlsafe(32),
        'replace-with-generated-local-internal-key': secrets.token_urlsafe(32),
        'replace-with-generated-database-password': secrets.token_hex(24),
    }
    for old,new in replacements.items(): text = text.replace(old,new)
    with target.open('x',encoding='utf-8',newline='\n') as stream: stream.write(text)
    print('Created ignored .env with local app credentials. No paid model API is configured.')
