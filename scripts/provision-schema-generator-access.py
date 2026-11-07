#!/usr/bin/env python3
"""Run on Dev. Default: read-only. --apply creates narrowly scoped access only.
Uses the official, already-authenticated Infisical CLI and Bridge operator API.
Never issues JWTs, changes budgets, grants subscriptions, deploys, or sends mail.
"""
import argparse, fcntl, json, os, secrets, shlex, subprocess, sys, tempfile
from pathlib import Path

WS = '7172a9a1-41b4-41c7-96d8-06629215eb87'
DOMAIN = 'http://100.79.71.99/api'
CWD = '/root/projekte/werkingflow-production'
BRIDGE_HOST = 'root@100.112.98.39'
NAME = 'schema-generator-dev'
APPS = ['werking-schema-generator']
PATHS = ['/v1/chat/completions']
STATE = Path('/root/.local/state/schema-generator-provision')
KEY = 'SCHEMA_CHECK_BRIDGE_API_KEY'
KONTO = 'CHECK_KONTO_SERVICE_SECRET'

class Stop(Exception): pass

def cli(args):
    p = subprocess.run(['infisical','--domain='+DOMAIN,'--silent']+args,
                       cwd=CWD, stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=40)
    if p.returncode:
        error = (p.stderr+'\n'+p.stdout).lower()
        if 'must be logged in' in error or 'login' in error:
            raise Stop('Infisical: regular CLI user login required')
        if '403' in error or 'permission' in error or 'unauthorized' in error:
            raise Stop('Infisical: access refused for requested scope')
        if '404' in error or 'secret not found' in error or 'secret does not exist' in error:
            return None
        raise Stop('Infisical CLI failed; no response body printed (exit '+str(p.returncode)+')')
    return p.stdout.strip()

def get_secret(env,key):
    return cli(['secrets','get',key,'--env='+env,'--projectId='+WS,'--path=/','--plain','--expand=false','--secret-overriding=false'])

def set_new_secret(env,key,value):
    existing=get_secret(env,key)
    if existing is not None:
        if not secrets.compare_digest(existing,value):
            raise Stop('Refusing to replace existing '+env+'/'+key)
        return
    # Normal CLI user auth; secret goes through a mode-0600 file, never argv.
    fd,name=tempfile.mkstemp(prefix='write-',suffix='.env',dir=STATE)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w') as f: f.write(key+'='+value+'\n')
        cli(['secrets','set','--env='+env,'--projectId='+WS,'--path=/','--type=shared','--file='+name])
    finally:
        os.unlink(name)
    actual=get_secret(env,key)
    if actual is None or not secrets.compare_digest(value,actual):
        raise Stop('Secret readback mismatch for '+env+'/'+key)

REMOTE = '''import os,sys,json,urllib.request,urllib.error
p=json.load(sys.stdin)
h={'X-Bridge-Service-Token':os.environ['BRIDGE_SERVICE_TOKEN'],'Content-Type':'application/json'}
r=urllib.request.Request('http://127.0.0.1:8000'+p['path'],headers=h,method=p['method'],data=json.dumps(p['body']).encode() if p.get('body') is not None else None)
try:
 with urllib.request.urlopen(r,timeout=20) as q: print(json.dumps({'status':q.status,'body':json.load(q)}))
except urllib.error.HTTPError as e: print(json.dumps({'status':e.code}))
'''

def bridge(method,path,body=None):
    command='docker exec -i wt-platform-api python -c '+shlex.quote(REMOTE)
    p=subprocess.run(['ssh','-o','ConnectTimeout=8',BRIDGE_HOST,command],
        input=json.dumps(dict(method=method,path=path,body=body)),capture_output=True,text=True,timeout=40)
    if p.returncode: raise Stop('Bridge transport failed; stderr suppressed')
    try: d=json.loads(p.stdout)
    except ValueError: raise Stop('Bridge returned unreadable response')
    if d.get('status') not in (200,201): raise Stop('Bridge HTTP '+str(d.get('status')))
    return d.get('body',{})

def check_scope(principal):
    if sorted(principal.get('allowed_apps',[])) != APPS or sorted(principal.get('allowed_paths',[])) != PATHS:
        raise Stop('Existing principal has different scope; not changed')
    if principal.get('monthly_cap_eur') is not None:
        raise Stop('Existing principal has an anonymous budget cap; not changed')

def dev_only_declared():
    p=subprocess.run(['ssh','-o','ConnectTimeout=8',BRIDGE_HOST,
        'cat /root/werkingflow-bridge/scripts/principals-dev-only.txt'],capture_output=True,text=True,timeout=15)
    if p.returncode or NAME not in [x.split('#',1)[0].strip() for x in p.stdout.splitlines()]:
        raise Stop('Deploy reviewed principals-dev-only.txt declaration before principal creation')

def main():
    a=argparse.ArgumentParser(description=__doc__);a.add_argument('--apply',action='store_true');args=a.parse_args()
    # Read-only probes first. An unreadable secret is NEVER treated as absent.
    schema=get_secret('dev',KEY);konto=get_secret('staging',KONTO)
    rows=bridge('GET','/v1/admin/service-principals').get('principals',[])
    found=[p for p in rows if p.get('name')==NAME]
    if len(found)>1: raise Stop('Duplicate principal names')
    principal=found[0] if found else None
    if principal: check_scope(principal)
    print(json.dumps({'mode':'apply' if args.apply else 'check','schema_secret_exists':schema is not None,
                      'konto_secret_exists':konto is not None,'principal_exists':principal is not None}))
    if not args.apply: return
    STATE.mkdir(parents=True,exist_ok=True);os.chmod(STATE,0o700)
    with open(STATE/'lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        dev_only_declared()
        pending=STATE/'pending-principal.json'
        created=False
        if principal and pending.exists():
            record=json.loads(pending.read_text())
            if record.get('name')!=NAME or record.get('id')!=principal.get('id'):
                raise Stop('Pending key belongs to different principal')
            if schema is not None and not secrets.compare_digest(schema,record['token']):
                raise Stop('Existing secret differs from recovery key; not reactivated')
        if principal:
            if schema is None:
                if not pending.exists(): raise Stop('Principal exists but no stored key. No automatic rotation.')
                record=json.loads(pending.read_text())
                if record.get('name')!=NAME or record.get('id')!=principal.get('id'): raise Stop('Pending key belongs to different principal')
                schema=record['token']
                set_new_secret('dev',KEY,schema)
            if not principal.get('active',False):
                if not pending.exists(): raise Stop('Existing principal inactive; no automatic reactivation')
                bridge('POST','/v1/admin/service-principals/set-active',dict(name=NAME,active=True))
        else:
            if schema is not None: raise Stop('Secret exists without matching principal; not overwritten')
            d=bridge('POST','/v1/admin/service-principals',dict(name=NAME,allowed_apps=APPS,allowed_paths=PATHS))
            schema=d.get('token')
            if not schema: raise Stop('Principal response missing token')
            fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w') as f: json.dump(dict(name=NAME,id=d.get('id'),token=schema),f)
            created=True
            try: set_new_secret('dev',KEY,schema)
            except Exception:
                bridge('POST','/v1/admin/service-principals/set-active',dict(name=NAME,active=False))
                raise Stop('Secret write failed. New principal deactivated; recovery key retained mode 0600.')
        if konto is None: set_new_secret('staging',KONTO,secrets.token_hex(32))
        if pending.exists(): pending.unlink()
        print('Access material stored and read back. Budgets, subscriptions, deployments and mail unchanged.')
        print('Still required: Partner MI read access, catalog/worker validation, Check-user grant and end-to-end acceptance.')

if __name__=='__main__':
    try: main()
    except (Stop,OSError,subprocess.TimeoutExpired) as e:
        print('STOP: '+str(e),file=sys.stderr);sys.exit(1)
