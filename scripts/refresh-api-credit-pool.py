#!/usr/bin/env python3
"""Refresh verified promotional grants, not paid balances. Run on Dev via cron.

Private input has {accounts:[{id,org_id,api_key,cookies:[domain,name,value]}]}.
Requires curl_cffi in an isolated tools venv. Publishes atomically to the ONE
coordinator, over SSH. A failed account retains its old observation timestamp
and expires after one hour; a failed run never renews stale credit eligibility.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
from decimal import Decimal

from curl_cffi import requests


def refresh(source, previous):
    old={a['id']:a for a in previous.get('accounts',[])}
    result=[]
    for a in source['accounts']:
        if not a.get('api_key'):continue
        try:
            session=requests.Session(impersonate='chrome')
            for c in a['cookies']:
                session.cookies.set(c['name'],c['value'],domain=c['domain'],path='/')
            orgs=session.get('https://platform.claude.com/api/organizations',timeout=25)
            if orgs.status_code!=200 or not any(o.get('uuid')==a['org_id'] for o in orgs.json()):
                raise RuntimeError('Console identity could not be verified')
            observed=dt.datetime.now(dt.timezone.utc).isoformat()
            r=session.get('https://platform.claude.com/api/organizations/'+a['org_id']+'/prepaid/credits',timeout=25)
            if r.status_code!=200:raise RuntimeError('Console credits unavailable')
            d=r.json();grants=[]
            for g in d.get('promo_tranches',[]):
                if g.get('program_id')!='subscription_api_credit' or g.get('currency')!='USD':continue
                scope=g.get('scope',{})
                if scope.get('kind')!='scoped' or not any(x.get('product')=='api' for x in scope.get('applies_to',[])):continue
                if not g.get('expires_at') or not g.get('id'):continue
                grants.append({'id':g['id'],'expires_at':g['expires_at'],
                    'granted_usd':str(Decimal(g['granted_amount_minor_units'])/100),
                    'remaining_usd':str(Decimal(g['remaining_amount_minor_units'])/100)})
            result.append({'id':a['id'],'org_id':a['org_id'],'api_key':a['api_key'],
                           'observed_at':observed,'grants':grants})
            print(a['id']+': refreshed '+str(len(grants))+' promotional grants',flush=True)
        except Exception as exc:
            if a['id'] in old:result.append(old[a['id']])
            print(a['id']+': refresh failed ('+type(exc).__name__+'); prior timestamp retained',flush=True)
    return {'accounts':result}


REMOTE_WRITE = '''import sys,os,json,pathlib
p=pathlib.Path('/root/werkingflow-bridge/secrets/api-credit-pool.json')
data=json.load(sys.stdin)
assert isinstance(data.get('accounts'),list)
t=p.with_suffix('.tmp')
fd=os.open(t,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:json.dump(data,f)
os.replace(t,p)
print('Credit manifest installed atomically; accounts',len(data['accounts']))
'''


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--source',required=True)
    parser.add_argument('--state',required=True);parser.add_argument('--publish-host')
    args=parser.parse_args();state=pathlib.Path(args.state)
    source=json.loads(pathlib.Path(args.source).read_text())
    previous=json.loads(state.read_text()) if state.exists() else {'accounts':[]}
    data=refresh(source,previous)
    temp=state.with_suffix('.tmp');fd=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:json.dump(data,f)
    os.replace(temp,state)
    if args.publish_host:
        import shlex
        subprocess.run(['ssh',args.publish_host,'python3 -c '+shlex.quote(REMOTE_WRITE)],input=json.dumps(data),text=True,check=True)
