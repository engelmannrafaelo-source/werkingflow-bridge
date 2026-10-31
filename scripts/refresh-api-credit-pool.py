#!/usr/bin/env python3
"""Refresh verified promotional grants, not paid balances. Run on Dev via cron.

Private input has {accounts:[{id,org_id,api_key,cookies:[domain,name,value]}]}.
Requires curl_cffi in an isolated tools venv. Publishes atomically to the ONE
coordinator, over SSH. An account whose billing state cannot be verified is disabled immediately.
Only prepaid organizations with no other balance and no auto-reload are eligible.
"""
import argparse
import datetime as dt
import json
import os
import pathlib
import subprocess
from decimal import Decimal

from curl_cffi import requests


def free_only_block_reason(org, credits):
    """A key cannot select promotional versus purchased funds in one wallet."""
    if org.get('billing_type') != 'prepaid':
        return 'billing_not_verified_prepaid'
    required = ('amount_without_scoped_credits', 'auto_reload_settings', 'currency', 'pending_invoice_amount_cents')
    if any(k not in credits for k in required) or credits['currency'] != 'USD':
        return 'billing_state_unknown'
    if credits['auto_reload_settings'] is not None:
        return 'auto_reload_not_confirmed_off'
    try:
        other = Decimal(str(credits['amount_without_scoped_credits']))
        pending = Decimal(str(credits['pending_invoice_amount_cents'] or 0))
        if not other.is_finite() or not pending.is_finite():
            return 'billing_state_unknown'
        if other != 0 or pending != 0:
            return 'mixed_or_paid_credit_balance'
    except (ValueError, ArithmeticError, TypeError):
        return 'billing_state_unknown'
    return None


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
            org=next(o for o in orgs.json() if o.get('uuid')==a['org_id'])
            observed=dt.datetime.now(dt.timezone.utc).isoformat()
            r=session.get('https://platform.claude.com/api/organizations/'+a['org_id']+'/prepaid/credits',timeout=25)
            if r.status_code!=200:raise RuntimeError('Console credits unavailable')
            d=r.json();grants=[]
            blocked=free_only_block_reason(org,d)
            for g in d.get('promo_tranches',[]):
                if g.get('program_id')!='subscription_api_credit' or g.get('currency')!='USD':continue
                scope=g.get('scope',{})
                if scope.get('kind')!='scoped' or not any(x.get('product')=='api' for x in scope.get('applies_to',[])):continue
                if not g.get('expires_at') or not g.get('id'):continue
                grants.append({'id':g['id'],'expires_at':g['expires_at'],
                    'granted_usd':str(Decimal(g['granted_amount_minor_units'])/100),
                    'remaining_usd':str(Decimal(g['remaining_amount_minor_units'])/100)})
            result.append({'id':a['id'],'org_id':a['org_id'],'api_key':a['api_key'],
                           'observed_at':observed,'grants':grants,
                           'disabled':bool(blocked),'disabled_reason':blocked,
                           'billing_guard':'free_only_verified' if not blocked else 'blocked'})
            print(a['id']+': '+('blocked: '+blocked if blocked else 'free-only verified')+', '+str(len(grants))+' promotional grants',flush=True)
        except Exception as exc:
            if a['id'] in old:result.append({**old[a['id']], 'disabled':True, 'disabled_reason':'billing_verification_failed'})
            print(a['id']+': refresh failed ('+type(exc).__name__+'); account disabled',flush=True)
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
