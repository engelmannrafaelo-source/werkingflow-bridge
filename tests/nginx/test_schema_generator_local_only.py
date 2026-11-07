"""Real Lua routing regression for the Dev-only generator service principal.

No provider calls: an isolated OpenResty network serves local/peer markers.
The real picker sees a higher-priority peer and must retain normal local
capacity gates for generator text; image early routing remains local too.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('energy_probe', Path(__file__).with_name('test_energy_policy_local_only.py'))
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
VAR = 'schema_generator_policy_local_only'
APPS = ['werking-schema-generator', 'werking-schema-generator-x', 'WERKING-SCHEMA-GENERATOR', 'werking-report', '']

def block(text):
    m = re.search(r'^map \$http_x_app_id \$'+VAR+r' \{.*?^\}', text, re.S|re.M)
    return m.group(0) if m else ''

@pytest.mark.parametrize('hop', ['0','1'])
@pytest.mark.parametrize('energy', ['0','1'])
def test_exact_dev_scope_independent_of_energy_setting(tmp_path,hop,energy):
    gen = probe._generate(tmp_path,energy,hop)
    mapping = block(gen['primary'])
    assert '"~^werking-schema-generator$" 1;' in mapping
    assert 'default 0;' in mapping
    assert VAR not in gen['prod']
    if energy=='1' and hop=='1':
        assert gen['primary']==(ROOT/'docker/upstreams-primary.conf').read_text()
        assert gen['prod']==(ROOT/'docker/upstreams-prod.conf').read_text()

def run(tmp_path,topology,local,image):
    work=Path(tempfile.mkdtemp(dir=tmp_path)); work.chmod(0o755)
    mapping=block((ROOT/f'docker/upstreams-{topology}.conf').read_text())
    (work/'t.conf').write_text(probe._conf(mapping,probe._policy(local)))
    shutil.copytree(ROOT/'docker/lua',work/'lua');shutil.copytree(ROOT/'docker/lua-vendor',work/'lua-vendor')
    body=json.dumps({'messages':[{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,cHJvYmU='}}]}]}) if image else '{}'
    commands=['mkdir -p /tmp/logs','openresty -t -c /w/t.conf -p /tmp >/dev/null 2>&1 || exit 7','openresty -c /w/t.conf -p /tmp','sleep 0.3']
    for app in APPS:
        hdr=f"--header 'X-App-ID: {app}'" if app else ''
        commands.append(f"wget -qO- {hdr} --header 'Content-Type: application/json' --post-data='{body}' http://127.0.0.1:8080/v1/chat/completions || echo ERR")
    commands.append("grep -c 'unknown variable\\|\\[error\\]' /tmp/logs/error.log || true")
    result=subprocess.run(['docker','run','--rm','--network','none','-v',f'{work}:/w:ro',probe.IMAGE,'sh','-c','\n'.join(commands)],capture_output=True,text=True,timeout=120)
    assert result.returncode==0,(result.stdout+result.stderr)[-2000:]
    lines=result.stdout.strip().splitlines();assert len(lines)==len(APPS)+1
    assert lines[-1]=='0',result.stdout
    return lines[:-1]

@pytest.mark.skipif(not probe._docker_ok(),reason='OpenResty image unavailable')
@pytest.mark.parametrize('topology,local,image,want',[
 ('primary',True,False,['local','peer','peer','peer','peer']),
 ('primary',False,False,['unavailable:all_unavail','peer','peer','peer','peer']),
 ('prod',True,False,['peer']*5),
 ('primary',True,True,['local']*5),
 ('primary',False,True,['local']*5),
])
def test_real_routing_preserves_scope_capacity_and_vision(tmp_path,topology,local,image,want):
    assert run(tmp_path,topology,local,image)==want
