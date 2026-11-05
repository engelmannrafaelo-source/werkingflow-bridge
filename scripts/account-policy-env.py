#!/usr/bin/env python3
"""Deploy helper: receive existing authority URL/token on stdin; print no values."""
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit

def render(data):
    if set(data) != {"url", "token"}:
        raise ValueError("expected authority URL and token")
    url, token = data["url"], data["token"]
    if not isinstance(url, str) or not isinstance(token, str) or any(c.isspace() for c in url+token):
        raise ValueError("invalid policy configuration")
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("invalid authority URL")
    if not token or token in ("null", "NOT_FOUND"):
        raise ValueError("missing authority token")
    return "BRIDGE_ACCOUNT_POLICY_URL=" + url.rstrip("/") + "/v1/admin/bridge-account-policy\nBRIDGE_ACCOUNT_POLICY_TOKEN=" + token + "\n"

def main():
    dest=Path(sys.argv[1])
    if dest.name != "account-policy.env" or not dest.parent.is_dir():
        raise ValueError("invalid destination")
    text=render(json.load(sys.stdin))
    if dest.exists() and dest.read_text()==text:
        os.chmod(dest,0o600)
        print("account policy configuration already current")
        return
    fd, temp=tempfile.mkstemp(prefix=".account-policy-",dir=dest.parent)
    try:
        with os.fdopen(fd,"w") as f:
            f.write(text);f.flush();os.fsync(f.fileno())
        os.chmod(temp,0o600);os.replace(temp,dest)
    finally:
        if os.path.exists(temp):os.unlink(temp)
    print("account policy configuration reconciled")

if __name__=="__main__":
    try:main()
    except Exception as e:
        print("account policy configuration refused: "+type(e).__name__,file=sys.stderr)
        sys.exit(1)
