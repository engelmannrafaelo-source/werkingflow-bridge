#!/usr/bin/env python3
"""
Auto-refresh model registry from Anthropic /v1/models endpoint.

Pulls the live model list from Anthropic, compares with the registry, and
adds any new models. Newest model in each family becomes the default
(via dynamic _compute_family_defaults()).

Idempotent: safe to run repeatedly. Only modifies registry when changes detected.

Usage:
    python3 refresh_model_registry.py [--dry-run] [--rebuild]
"""
import argparse, os, re, subprocess, sys, urllib.request, json
from datetime import datetime, date
from pathlib import Path

REGISTRY_PATH = Path('/root/werkingflow-bridge/src/model_registry.py')
TOKEN_PATHS = [
    '/root/werkingflow-bridge/secrets/claude_token_prod.txt',
    '/root/werkingflow-bridge/secrets/claude_token_account1.txt',
    '/root/werkingflow-bridge/secrets/claude_token.txt',
]

def load_token():
    for p in TOKEN_PATHS:
        if os.path.exists(p):
            t = Path(p).read_text().strip()
            if t.startswith('sk-ant-oat'):
                return t
    raise SystemExit('No usable OAuth token found in bridge secrets')

def fetch_models(token: str) -> list:
    req = urllib.request.Request(
        'https://api.anthropic.com/v1/models',
        headers={
            'Authorization': f'Bearer {token}',
            'anthropic-beta': 'oauth-2025-04-20',
            'anthropic-version': '2023-06-01',
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())['data']

def parse_family(model_id: str) -> str | None:
    if 'opus' in model_id: return 'opus'
    if 'sonnet' in model_id: return 'sonnet'
    if 'haiku' in model_id: return 'haiku'
    return None

def parse_version(model_id: str, display_name: str) -> str:
    # 'Claude Opus 4.7' -> '4.7'
    m = re.search(r'(\d+\.\d+|\d+)', display_name)
    if m: return m.group(1)
    return ''

def get_registered_ids(content: str) -> set[str]:
    return set(re.findall(r'id="(claude-[^"]+)"', content))

def insert_model_entry(content: str, model: dict) -> str:
    family = parse_family(model['id'])
    if not family:
        return content
    version = parse_version(model['id'], model.get('display_name',''))
    rd_str = model.get('created_at','')[:10]
    try:
        rd = datetime.strptime(rd_str, '%Y-%m-%d').date()
    except:
        rd = date.today()
    desc = model.get('display_name', model['id'])
    entry = f'''    ModelInfo(
        id="{model['id']}",
        family="{family}",
        version="{version}",
        release_date=date({rd.year}, {rd.month}, {rd.day}),
        description="{desc}"
    ),
'''
    # Insert before the family's last entry — lookup any existing entry of this family
    pattern = re.compile(rf'(    # {family.capitalize()} Familie\n)', re.IGNORECASE)
    if pattern.search(content):
        return pattern.sub(rf'\1{entry}', content, count=1)
    # Fallback: insert at end of MODELS list
    return content.replace('\n]\n', f'\n{entry}]\n', 1)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--rebuild', action='store_true', help='Run docker rebuild + recreate after changes')
    args = ap.parse_args()

    token = load_token()
    live = fetch_models(token)
    print(f'[refresh] Anthropic returned {len(live)} models')
    content = REGISTRY_PATH.read_text()
    registered = get_registered_ids(content)
    new_ids = [m for m in live if m['id'] not in registered and parse_family(m['id'])]
    if not new_ids:
        print('[refresh] no new Claude models to add')
        return 0
    print(f'[refresh] {len(new_ids)} new Claude models detected:')
    for m in new_ids:
        print(f'    + {m["id"]} ({m.get("display_name","")}, {m.get("created_at","")[:10]})')
    if args.dry_run:
        print('[refresh] dry-run — no changes written')
        return 0
    backup = REGISTRY_PATH.with_suffix(f'.py.bak-{datetime.now():%Y%m%d-%H%M%S}')
    backup.write_text(content)
    print(f'[refresh] backup: {backup}')
    for m in new_ids:
        content = insert_model_entry(content, m)
    REGISTRY_PATH.write_text(content)
    print('[refresh] registry updated')
    if args.rebuild:
        print('[refresh] rebuilding workers...')
        subprocess.check_call(['docker','compose','-f','/root/werkingflow-bridge/docker/docker-compose.yml','build','worker1','worker2','worker3','worker4'])
        subprocess.check_call(['docker','compose','-f','/root/werkingflow-bridge/docker/docker-compose.yml','up','-d','--force-recreate','worker1','worker2','worker3','worker4'])
        print('[refresh] rebuild + recreate done')
    return 0

if __name__ == '__main__':
    sys.exit(main())
