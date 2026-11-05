#!/usr/bin/env python3
"""One approved rollout, 2026-11-06 01:05 UTC; no scheduler, retries or bypasses.

--check is read-only (including local files); --run records private phase logs
and a terminal status. A consumed run directory cannot be reused. Root reviews
and installs the one-shot timer separately. Completion means consumer-ready,
not CUI or Unified Tester activation.
"""
import argparse
import datetime as dt
import fcntl
import json
import math
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

START = dt.datetime(2026, 11, 6, 1, 5, tzinfo=dt.timezone.utc).timestamp()
STATE_DIR = Path('/root/bridge-policy-rollout-20261106')
DEV = '49.12.72.66'
REPO = '/root/werkingflow-bridge'
BASE_DEV = '1f696abaebe3e94301e6851771a56840b06efd3a'
BASE_PROD = '929e5720a04b448e0692a518bfe662ebccf47d0d'
CORE = ['platform-api', 'metrics-reader', 'nginx', 'worker1', 'worker2', 'worker3', 'worker4']
STAGES = [
    ('bootstrap-dev', '/root/worktrees/bridge-policy-bootstrap-root-dev-20261105',
     '0683551f518e537aef2436b25a603a708ef19151', 'release/bridge-policy-bootstrap-dev-20261105', 'hetzner'),
    ('bootstrap-prod', '/root/worktrees/bridge-policy-bootstrap-root-prod-20261105',
     'fa3f2a00076c85840efc141f2ed004d1f9b6f207', 'release/bridge-policy-bootstrap-prod-20261105', 'prod-zusammen'),
    ('full-dev', '/root/worktrees/bridge-account-policy-root-dev-20261105',
     'ae85aafff1954365fe83de3fb6b220431473375a', 'release/bridge-account-policy-dev-20261105', 'hetzner'),
    ('full-prod', '/root/worktrees/bridge-account-policy-root-prod-20261105',
     '44ce58d5d8ff19cc71e0d6c34aebc0b7a05fb774', 'release/bridge-account-policy-prod-20261105', 'prod-zusammen'),
]
HOSTS = [
    (DEV, BASE_DEV, ['wt-platform-api', 'wt-wrapper-metrics-reader', 'wt-wrapper-lb'] +
     ['wt-wrapper-worker' + str(i) for i in range(1, 5)]),
    ('178.104.178.79', BASE_PROD, ['wt-prod-platform-api', 'wt-prod-lb']),
    ('100.93.143.105', BASE_PROD, ['wt-worker-host-metrics-reader'] +
     ['wt-worker-host-' + a for a in ['sahori', 'kurt', 'coach', 'erk']]),
]


class Stop(RuntimeError):
    pass


class CapacityNotReady(Stop):
    pass


def capture(args, *, cwd=None, input=None):
    result = subprocess.run(args, cwd=cwd, input=input, text=True,
                            capture_output=True, timeout=45)
    if result.returncode:
        # Never echo credentials, remote stderr or environment.
        raise Stop(f'read-only command failed: {args[0]} exit {result.returncode}')
    return result.stdout.strip()


def ssh(host, args, input=None):
    return capture(['ssh', '-i', '/root/.ssh/id_ed25519', '-o', 'BatchMode=yes',
                    '-o', 'ConnectTimeout=10', 'root@' + host, shlex.join(args)], input=input)


def check_stage(stage):
    name, path, sha, ref, _ = stage
    def git(*args):
        return capture(['git', '-C', path, *args])
    if git('rev-parse', 'HEAD') != sha or git('status', '--porcelain', '--untracked-files=no'):
        raise Stop(name + ': HEAD changed or tracked worktree dirty')
    if git('rev-parse', 'origin/' + ref) != sha:
        raise Stop(name + ': local remote-tracking release ref changed')
    expected = sha + '\trefs/heads/' + ref
    if git('ls-remote', '--exit-code', 'origin', 'refs/heads/' + ref) != expected:
        raise Stop(name + ': authoritative remote release ref changed')


def check_baseline():
    for host, sha, containers in HOSTS:
        if ssh(host, ['git', '-C', REPO, 'rev-parse', 'HEAD']) != sha:
            raise Stop(host + ': live checkout baseline changed')
        if ssh(host, ['git', '-C', REPO, 'status', '--porcelain', '--untracked-files=no']):
            raise Stop(host + ': live tracked checkout dirty')
        # Inspect only labels/state; never dump container environments.
        for container in containers:
            raw = ssh(host, ['docker', 'inspect', '--format',
                '{{json .State}}\n{{index .Config.Labels "bridge.git.commit"}}', container])
            state_raw, image_sha = raw.rsplit('\n', 1)
            state = json.loads(state_raw)
            if image_sha != sha or not state.get('Running') or state.get('Health', {}).get('Status') != 'healthy':
                raise Stop(host + '/' + container + ': live image baseline or health changed')


def check_pool(now=None):
    now = time.time() if now is None else now
    # Public read-only local metrics endpoint; no provider call.
    with urllib.request.urlopen('http://' + DEV + ':8000/v1/metrics/account-pool-state', timeout=15) as response:
        if response.status != 200:
            raise Stop('Dev pool did not return HTTP 200')
        pool = json.load(response)
    stamp = pool.get('ts')
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or not 0 <= now - stamp <= 120:
        raise Stop('Dev pool timestamp stale, future or absent')
    eligible = []
    for name, row in pool.get('accounts', {}).items():
        weekly, cap, flight = row.get('weekly_percent'), row.get('effective_cap_tokens'), row.get('current_in_flight_tokens')
        valid = all(type(v) in (int, float) and math.isfinite(v) for v in [weekly, cap, flight])
        if (valid and row.get('available') is True and row.get('usage_known') is True
                and 0 <= weekly < 90 and cap > flight >= 0
                and row.get('bridge_enabled', True) is True):
            eligible.append(name)
    if not eligible:
        raise CapacityNotReady('Dev pool has no measured available local account below 90 percent')
    return {'pool_ts': stamp, 'eligible_local_accounts': eligible}


def preflight():
    for stage in STAGES:
        check_stage(stage)
    check_baseline()
    return check_pool()


AUTHORITY_PROBE = '''import os,json,urllib.request,sys
try:
    url=os.environ['BRIDGE_ACCOUNT_POLICY_URL']
    token=os.environ['BRIDGE_ACCOUNT_POLICY_TOKEN']
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self,*args,**kwargs): return None
    opener=urllib.request.build_opener(NoRedirect)
    req=urllib.request.Request(url,headers={'X-Bridge-Service-Token':token})
    with opener.open(req,timeout=10) as r:
        if r.status != 200: raise ValueError('status')
        p=json.load(r)
    from src.account_policy.model import validate
    if p.get('source') != 'bridge-account-policy': raise ValueError('source')
    p=validate(p)
    if len(p['accounts']) != 8: raise ValueError('accounts')
    print(json.dumps({'http_status':200,'source':p['source'],'account_count':8,'revision':p['revision']}))
except Exception as exc:
    print(json.dumps({'error':'authority proof failed','type':type(exc).__name__}))
    sys.exit(1)
'''


def authority_proof():
    raw = ssh(DEV, ['docker', 'exec', '-i', 'wt-wrapper-worker1', '/app/.venv/bin/python', '-'], AUTHORITY_PROBE)
    proof = json.loads(raw)
    if proof.get('http_status') != 200 or proof.get('source') != 'bridge-account-policy' or proof.get('account_count') != 8:
        raise Stop('authority proof invalid')
    return proof


def write_status(directory, status):
    temp = directory / 'status.tmp'
    with temp.open('w') as stream:
        json.dump(status, stream, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(directory / 'status.json')


def deploy(stage, directory):
    name, path, sha, ref, target = stage
    # Only the normal authenticated deployment flow. Do not inherit FORCE/SKIP
    # or other gate-changing caller variables into the child.
    env = {k: v for k, v in os.environ.items() if k in ('PATH', 'HOME', 'USER', 'LOGNAME', 'LANG', 'SSH_AUTH_SOCK')}
    env['DEPLOY_REF'] = 'origin/' + ref
    if target == 'hetzner':
        env['AI_BRIDGE_URL'] = 'http://' + DEV + ':8000'
    args = ['bash', str(Path(path) / 'scripts/bridge-deploy.sh'), target, '--ack-foreign']
    if target == 'hetzner':
        args.extend(CORE)
    with (directory / (name + '.log')).open('x') as log:
        log.write(json.dumps({'phase': name, 'sha': sha, 'command': args, 'release_ref': ref}) + '\n')
        log.flush()
        return subprocess.run(args, cwd=path, env=env, stdout=log, stderr=subprocess.STDOUT).returncode


def run_once(directory=STATE_DIR):
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_mode & 0o077:
        raise Stop('run directory must be private and not a symlink')
    with (directory / 'orchestration.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Stop('another orchestration holds the lock')
        if (directory / 'status.json').exists():
            raise Stop('one-shot already consumed; inspect existing status, no automatic retry')
        status = {'state': 'checking', 'started_at': time.time(), 'phases': [], 'scope': 'bridge-consumers-ready only; hand off to Tester owner, no restart'}
        write_status(directory, status)
        try:
            if time.time() < START:
                raise Stop('scheduled time not reached: 2026-11-06 01:05 UTC')
            status['preflight'] = preflight()
            write_status(directory, status)
            for stage in STAGES:
                check_stage(stage)  # Recheck pinned source immediately before each deploy.
                if stage[4] == 'hetzner':
                    status['latest_dev_pool'] = check_pool()
                status['state'] = 'deploying'
                phase = {'name': stage[0], 'sha': stage[2], 'started_at': time.time(), 'exit_code': None}
                status['phases'].append(phase)
                write_status(directory, status)
                rc = deploy(stage, directory)
                phase.update(exit_code=rc, ended_at=time.time())
                write_status(directory, status)
                if rc != 0:
                    raise Stop(f'{stage[0]} exit {rc}; stopping without retry')
                if stage[0] == 'bootstrap-prod':
                    status['authority'] = authority_proof()
                    write_status(directory, status)
            status['state'] = 'bridge-consumers-ready'
            status['ended_at'] = time.time()
            write_status(directory, status)
            return 0
        except Exception as exc:
            status.update(state='stopped', error=str(exc) if isinstance(exc, Stop) else type(exc).__name__, ended_at=time.time())
            write_status(directory, status)
            return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--run', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps({'state': 'check-ready', 'earliest_run_utc': '2026-11-06T01:05:00Z', **preflight()}))
            return 0
        return run_once()
    except Exception as exc:
        print(json.dumps({'state': 'stopped', 'error': str(exc) if isinstance(exc, Stop) else type(exc).__name__}))
        return 3 if isinstance(exc, CapacityNotReady) else 1


if __name__ == '__main__':
    sys.exit(main())
