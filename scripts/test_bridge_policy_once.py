"""Offline orchestration checks. No provider, deployment or live SSH calls."""
import io
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge_policy_once as once


def response(pool):
    result = Mock(status=200)
    result.read = lambda: json.dumps(pool).encode()
    result.__enter__ = Mock(return_value=result)
    result.__exit__ = Mock(return_value=False)
    return result


def pool(**row):
    account = dict(available=True, usage_known=True, weekly_percent=0,
                   effective_cap_tokens=1000, current_in_flight_tokens=0)
    account.update(row)
    return {'ts': 1000, 'accounts': {'engelmann': account}}


class OnceTest(unittest.TestCase):
    def test_pool_guard(self):
        for change in [dict(available=False), dict(usage_known=False), dict(weekly_percent=90),
                       dict(effective_cap_tokens=0), dict(bridge_enabled=False)]:
            with self.subTest(change=change), patch('urllib.request.urlopen', return_value=response(pool(**change))):
                self.assertRaises(once.CapacityNotReady, once.check_pool, 1001)
        with patch('urllib.request.urlopen', return_value=response(pool())):
            self.assertEqual(once.check_pool(1001)['eligible_local_accounts'], ['engelmann'])
            self.assertRaises(once.Stop, once.check_pool, 1121)
            self.assertRaises(once.Stop, once.check_pool, 999)

    def test_source_guards_subprocess(self):
        stage = once.STAGES[0]
        good = [stage[2], '', stage[2], stage[2] + '\trefs/heads/' + stage[3]]
        with patch('subprocess.run', side_effect=[Mock(returncode=0, stdout=x) for x in good]):
            once.check_stage(stage)
        for index in range(4):
            bad = good.copy()
            bad[index] = 'changed'
            with self.subTest(index=index), patch('subprocess.run', side_effect=[Mock(returncode=0, stdout=x) for x in bad]):
                self.assertRaises(once.Stop, once.check_stage, stage)

    def test_baseline_guard(self):
        host = once.HOSTS[0]
        with patch.object(once, 'HOSTS', [host]), patch.object(once, 'ssh', return_value='wrong'):
            self.assertRaises(once.Stop, once.check_baseline)
        states = [host[1], ''] + [json.dumps({'Running': True, 'Health': {'Status': 'healthy'}}) + '\n' + host[1]] * len(host[2])
        with patch.object(once, 'HOSTS', [host]), patch.object(once, 'ssh', side_effect=states):
            once.check_baseline()
        states[2] = json.dumps({'Running': True, 'Health': {'Status': 'healthy'}}) + '\nwrong-image'
        with patch.object(once, 'HOSTS', [host]), patch.object(once, 'ssh', side_effect=states):
            self.assertRaises(once.Stop, once.check_baseline)

    def run_pipeline(self, codes, authority_error=False):
        calls = []
        temp = tempfile.TemporaryDirectory()
        directory = Path(temp.name) / 'run'
        def process(args, **kwargs):
            calls.append(args)
            self.assertNotIn('SKIP_DIST_TEST', kwargs['env'])
            self.assertNotIn('FORCE_DEPLOY', kwargs['env'])
            return Mock(returncode=codes[len(calls)-1])
        def authority():
            calls.append('authority')
            if authority_error:
                raise once.Stop('authority failed')
            return {'http_status': 200, 'source': 'bridge-account-policy', 'account_count': 8}
        # Keep deployment subprocess itself mocked; record authority independently.
        with patch.object(once, 'preflight', return_value={}), patch.object(once, 'check_stage'), patch.object(once, 'check_pool', return_value={}), patch.object(once, 'authority_proof', side_effect=authority), patch('time.time', return_value=once.START+1), patch('subprocess.run', side_effect=process), patch.dict(os.environ, {'SKIP_DIST_TEST': 'true', 'FORCE_DEPLOY': 'true'}):
            rc = once.run_once(directory)
        status = json.loads((directory/'status.json').read_text())
        self.assertEqual(directory.stat().st_mode & 0o077, 0)
        self.assertEqual((directory/'status.json').stat().st_mode & 0o077, 0)
        self.assertRaises(once.Stop, once.run_once, directory)
        temp.cleanup()
        return rc, calls, status

    def test_success_order_and_scope(self):
        rc, calls, status = self.run_pipeline([0, 0, 0, 0, 0])
        self.assertEqual(rc, 0)
        self.assertEqual(calls[2], 'authority')
        commands = [c for c in calls if isinstance(c, list)]
        self.assertEqual([c[2] for c in commands], ['hetzner', 'prod-zusammen', 'hetzner', 'prod-zusammen'])
        self.assertTrue(all('--ack-foreign' in c for c in commands))
        self.assertEqual(commands[0][4:], once.CORE)
        self.assertEqual(status['state'], 'bridge-consumers-ready')
        self.assertEqual([p['exit_code'] for p in status['phases']], [0,0,0,0])

    def test_each_nonzero_stops(self):
        for code in [1, 2, 3, -15]:
            rc, calls, status = self.run_pipeline([code])
            self.assertEqual(rc, 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(status['phases'][0]['exit_code'], code)

    def test_authority_failure_stops_before_full(self):
        rc, calls, status = self.run_pipeline([0, 0], authority_error=True)
        self.assertEqual(rc, 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(status['phases']), 2)

    def test_lock_and_preflight_failure_do_not_deploy(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            with (directory/'orchestration.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(once, 'deploy') as deploy:
                    self.assertRaises(once.Stop, once.run_once, directory)
                    deploy.assert_not_called()
            with patch('time.time', return_value=once.START+1), patch.object(once, 'preflight', side_effect=once.Stop('baseline changed')), patch.object(once, 'deploy') as deploy:
                self.assertEqual(once.run_once(directory), 1)
                deploy.assert_not_called()
            self.assertEqual(json.loads((directory/'status.json').read_text())['error'], 'baseline changed')

    def test_check_capacity_exit3(self):
        with patch('sys.argv', ['once', '--check']), patch.object(once, 'preflight', side_effect=once.CapacityNotReady('no capacity')), patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(once.main(), 3)


if __name__ == '__main__':
    unittest.main()
