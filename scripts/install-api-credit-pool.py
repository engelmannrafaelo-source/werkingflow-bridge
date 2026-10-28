#!/usr/bin/env python3
"""Install private credit-pool credentials and the refresh job FROM the repo.

Run on Dev. No service restarts: activate only through bridge-deploy.sh from
the verified commit. Existing secrets are retained; output never contains keys.
"""
import argparse
import json
import os
import pathlib
import secrets
import shlex
import shutil
import subprocess

REMOTE = r'''
import json,sys,pathlib,os,shutil,datetime
d=json.load(sys.stdin)
p=pathlib.Path('/root/werkingflow-bridge/secrets/platform.env')
assert p.exists(), 'Existing platform.env is required'
backup=p.with_name('platform.env.before-credit-pool')
if not backup.exists():shutil.copy2(p,backup);backup.chmod(0o600)
values={'ANTHROPIC_CREDIT_POOL_URL':d['url'],'ANTHROPIC_CREDIT_POOL_TOKEN':d['token']}
lines=[x for x in p.read_text().splitlines() if x.partition('=')[0] not in values]
lines.extend(k+'='+v for k,v in values.items())
tmp=p.with_suffix('.credit-pool-tmp');tmp.write_text(chr(10).join(lines)+chr(10));tmp.chmod(p.stat().st_mode & 0o777)
os.chown(tmp,p.stat().st_uid,p.stat().st_gid);os.replace(tmp,p)
print('Credit client installed; runtime activation requires repository deploy')
'''

REMOTE_GPU = r'''
import json,sys,pathlib,os
d=json.load(sys.stdin)
p=pathlib.Path('/root/werkingflow-bridge/secrets/api-credit-pool-client.env')
p.parent.mkdir(exist_ok=True)
fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
with os.fdopen(fd,'w') as f:
 f.write('ANTHROPIC_CREDIT_POOL_URL='+d['url']+chr(10)+'ANTHROPIC_CREDIT_POOL_TOKEN='+d['token']+chr(10))
print('GPU credit client staged; activation requires gpu-privacy deploy')
'''


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',required=True)
    parser.add_argument('--coordinator-host',required=True)
    parser.add_argument('--coordinator-url',required=True)
    parser.add_argument('--coordinator-local-url',default='http://platform-api:8000')
    parser.add_argument('--worker-host',action='append',default=[])
    parser.add_argument('--gpu-host')
    args=parser.parse_args()
    if os.getuid()!=0:raise SystemExit('Run on Dev as root')
    root=pathlib.Path('/root/.local/share/bridge-api-credit-pool');root.mkdir(mode=0o700,parents=True,exist_ok=True)
    config=root/'console-accounts.json'
    data=json.loads(pathlib.Path(args.source).read_text())
    assert data.get('accounts'), 'No verified accounts'
    config.write_text(json.dumps(data));config.chmod(0o600)
    tokenfile=root/'coordinator-token'
    if not tokenfile.exists():tokenfile.write_text(secrets.token_urlsafe(48));tokenfile.chmod(0o600)
    token=tokenfile.read_text().strip()
    assert args.coordinator_url.startswith('http://100.') or args.coordinator_url.startswith('https://')
    for host in dict.fromkeys([args.coordinator_host,*args.worker_host]):
        subprocess.run(['ssh',host,'python3 -c '+shlex.quote(REMOTE)],input=json.dumps({'url':args.coordinator_local_url if host == args.coordinator_host else args.coordinator_url,'token':token}),text=True,check=True)
    if args.gpu_host:
        subprocess.run(['sudo','-n','-u','claude-user','ssh',args.gpu_host,'python3 -c '+shlex.quote(REMOTE_GPU)],input=json.dumps({'url':args.coordinator_url,'token':token}),text=True,check=True)
    script=root/'refresh.py';shutil.copy2(pathlib.Path(__file__).with_name('refresh-api-credit-pool.py'),script)
    python=root/'venv/bin/python'
    if not python.exists():subprocess.run(['uv','venv',str(root/'venv'),'--python','3.12'],check=True)
    subprocess.run(['uv','pip','install','--python',str(python),'curl-cffi==0.13.0'],check=True)
    cmd=[str(python),str(script),'--source',str(config),'--state',str(root/'manifest.json'),'--publish-host',args.coordinator_host]
    subprocess.run(cmd,check=True)
    log=root/'refresh.log';log.touch(mode=0o600,exist_ok=True)
    cron=pathlib.Path('/etc/cron.d/bridge-api-credit-pool')
    cron.write_text('SHELL=/bin/bash\nPATH=/usr/local/bin:/usr/bin:/bin\n*/10 * * * * root flock -n '+str(root/'refresh.lock')+' '+shlex.join(cmd)+' >> '+str(log)+' 2>&1\n')
    cron.chmod(0o644)
    print('Refresh installed every 10 minutes; no containers changed')


if __name__=='__main__':main()
