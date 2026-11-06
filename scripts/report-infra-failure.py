#!/usr/bin/env python3
"""Authenticated, durable notification to the existing Bridge deploy owner.

Delivery is not repair acceptance. No models, sessions or retries are started.
The deploy retains its rollback decision independently of this hook.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    summary = next((line for line in os.getenv('SMOKE_OUTPUT', '').splitlines()
                    if line.startswith('SMOKE_FAIL:')), None)
    if summary is None:
        print('infra-fix: no smoke failure; no notification')
        return 0
    # Only the summary enters the incident; full smoke bodies/repros can contain
    # caller material and remain in the existing protected deployment log.
    summary = summary[:1000]
    label = os.getenv('SMOKE_LABEL', 'unknown')[:100]
    base = os.getenv('CUI_API', 'http://localhost:4005').rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1')
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path):
        print('infra-fix: invalid local CUI_API', file=sys.stderr)
        return 1
    token_result = subprocess.run(['cui-token'], capture_output=True, text=True)
    token = token_result.stdout.strip() if token_result.returncode == 0 else ''
    if not token:
        print('infra-fix: CUI_USER_TOKEN unavailable; authenticated deploy-owner channel required', file=sys.stderr)
        return 1
    try:
        raw = token.split('.')[1]
        caller = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))['sitzung']
    except (IndexError, KeyError, ValueError):
        print('infra-fix: session identity missing from own credential', file=sys.stderr)
        return 1
    owner = os.getenv('INFRA_FIX_SESSION_ID', caller)
    if not re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', owner):
        print('infra-fix: invalid INFRA_FIX_SESSION_ID', file=sys.stderr)
        return 1
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    internal = os.getenv('CUI_PARTNER_INTERNAL_TOKEN')
    if internal:
        headers['x-cui-internal-token'] = internal
    directory = Path(os.getenv('XDG_STATE_HOME', str(Path.home() / '.local/state'))) / 'bridge-infra-fix'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
        print('infra-fix: unsafe incident state directory', file=sys.stderr)
        return 1
    key = hashlib.sha256((owner + '\n' + label + '\n' + summary).encode()).hexdigest()
    path = directory / (key + '.json')
    with (directory / (key + '.lock')).open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists():
            print('infra-fix: existing incident ' + key + '; inspect receipt before any resend')
            return 0
        state = {'incidentId': key, 'ownerSessionId': owner, 'createdAt': time.time(),
                 'label': label, 'summary': summary, 'state': 'sending', 'accepted': False}
        def persist():
            temp = path.with_suffix('.tmp')
            with temp.open('w') as stream:
                json.dump(state, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
        persist()
        message = ('Bridge-Deployfehler im bestehenden Betriebsauftrag. Incident ' + key +
                   ', Profil ' + label + ': ' + summary +
                   '\nBitte tatsächliche Reparaturannahme und Ergebnis im Originalauftrag mit dieser Incident-ID belegen.'
                   ' Der Deploy entscheidet selbst über Rückbau; diese Nachricht ist kein Rollout- oder Modellstart.'
                   ' Kein zweiter Deployowner. Zustellung ist keine Annahme.')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        try:
            request = urllib.request.Request(base + '/api/mission/send', headers=headers,
                data=json.dumps({'sessionId': owner, 'message': message}).encode())
            with opener.open(request, timeout=20) as response:
                result = json.load(response)
            if result.get('ok') is not True:
                state.update(state='rejected', error='cui_not_ok')
            else:
                state.update(state='delivered_pending_acceptance',
                    deliveryState=result.get('deliveryState'), received=result.get('received'),
                    receipt=result.get('hofZustellbeleg') or result.get('amtsZustellbeleg'))
        except urllib.error.HTTPError as error:
            state.update(state='rejected', httpStatus=error.code)
        except (OSError, ValueError, urllib.error.URLError):
            state.update(state='delivery_uncertain', error='transport_or_response')
        persist()
        print('infra-fix: incident=' + key + ' state=' + state['state'] + ' accepted=false')
        return 0 if state['state'] == 'delivered_pending_acceptance' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError):
        print('infra-fix: local state/credential operation failed; no acceptance claimed', file=sys.stderr)
        sys.exit(1)
