import importlib.util
from pathlib import Path
import subprocess
import sys
import json

def test_policy_env_is_atomic_private_and_rejects_invalid_input(tmp_path):
    script=Path(__file__).parents[1]/"scripts/account-policy-env.py"
    dest=tmp_path/"account-policy.env"
    good={"url":"https://authority.test","token":"synthetic-token"}
    result=subprocess.run([sys.executable,str(script),str(dest)],input=json.dumps(good),text=True,capture_output=True)
    assert result.returncode==0
    assert "synthetic-token" not in result.stdout+result.stderr
    assert dest.stat().st_mode & 0o777 == 0o600
    original=dest.read_bytes()
    bad=dict(good,token="bad\nINJECT=1")
    result=subprocess.run([sys.executable,str(script),str(dest)],input=json.dumps(bad),text=True,capture_output=True)
    assert result.returncode==1 and dest.read_bytes()==original
