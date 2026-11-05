"""Offline checks for deploy account preselection; never contacts a provider."""
import contextlib
import io
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import requests
import bridge_smoke as smoke
from bridge_smoke_accounts import local_accounts, PoolStateError


def row(rank=1, enabled=True, **extra):
    return dict(bridge_enabled=enabled, bridge_priority=rank, bridge_policy_revision=4,
                available=True, cooldown_remaining_s=0, effective_cap_tokens=2000,
                current_in_flight_tokens=0, usage_known=True, weekly_percent=20, **extra)


def response(accounts=None, legacy=False):
    state = {'accounts': accounts if accounts is not None else {'one': row()}}
    if not legacy:
        state['bridge_policy_revision'] = 4
    return Mock(status_code=200, json=lambda: state, raise_for_status=lambda: None)


class AccountsTest(unittest.TestCase):
    def test_disabled_priority_and_capacity(self):
        accounts = {'disabled': row(1, False), 'later': row(3), 'first': row(2), 'wall': row(4)}
        accounts['wall']['weekly_percent'] = 96
        with patch('requests.get', return_value=response(accounts)):
            self.assertEqual(local_accounts('http://local', {}), ['first', 'later'])

    def test_no_eligible_and_legacy(self):
        with patch('requests.get', return_value=response({'off': row(enabled=False)})):
            self.assertEqual(local_accounts('http://local', {}), [])
        with patch('requests.get', return_value=response({'old': {'available': True}}, legacy=True)):
            self.assertIsNone(local_accounts('http://local', {}))

    def test_fetch_and_partial_policy_fail_closed(self):
        with patch('requests.get', side_effect=requests.ConnectionError):
            self.assertRaises(PoolStateError, local_accounts, 'http://local', {})
        with patch('requests.get', return_value=response({'partial': {'bridge_enabled': True}})):
            self.assertRaises(PoolStateError, local_accounts, 'http://local', {})

    def test_runner_no_request_and_unproven(self):
        with patch.object(smoke, 'resolve_api_key', return_value='test'), patch('requests.get', return_value=response({'off': row(enabled=False)})), patch('requests.post') as post:
            results = smoke.run('http://local', 'hetzner', 'chat_completions', {}, 1, expect_bridge='dev')
            post.assert_not_called()
            passed, gaps, failures = smoke.partition_results(results)
            self.assertFalse(passed or failures)
            self.assertTrue(gaps[0].capacity_preflight)

    def test_runner_hint_and_wrong_served_by_stays_red(self):
        answer = Mock(status_code=200, headers={'X-Bridge-Served-By': 'prod/worker'}, json=lambda: {'choices': [{}]})
        with patch.object(smoke, 'resolve_api_key', return_value='test'), patch('requests.get', return_value=response()), patch('requests.post', return_value=answer) as post:
            results = smoke.run('http://local', 'hetzner', 'chat_completions', {}, 1, expect_bridge='dev')
            self.assertEqual(post.call_args.kwargs['headers']['X-Bridge-Account'], 'one')
            self.assertTrue(smoke.partition_results(results)[2])

    def test_runner_legacy_keeps_existing_path(self):
        answer = Mock(status_code=200, headers={'X-Bridge-Served-By': 'dev/worker'}, json=lambda: {'choices': [{}]})
        with patch.object(smoke, 'resolve_api_key', return_value='test'), patch('requests.get', return_value=response({'old': {}}, legacy=True)), patch('requests.post', return_value=answer) as post:
            results = smoke.run('http://local', 'hetzner', 'chat_completions', {'X-Bridge-Hop': '1'}, 1, expect_bridge='dev')
            self.assertTrue(results[0].ok)
            self.assertEqual(post.call_args.kwargs['headers']['X-Bridge-Hop'], '1')
            self.assertNotIn('X-Bridge-Account', post.call_args.kwargs['headers'])

    def test_runner_fetch_failure_no_provider(self):
        with patch.object(smoke, 'resolve_api_key', return_value='test'), patch('requests.get', side_effect=requests.ConnectionError), patch('requests.post') as post:
            results = smoke.run('http://local', 'hetzner', 'chat_completions', {}, 1)
            post.assert_not_called()
            self.assertTrue(smoke.partition_results(results)[2])

    def distribution(self, states, served='dev/worker', status=200):
        script = Path(__file__).with_name('bridge-deploy.sh').read_text()
        code = script.split('DIST_SCRIPTS=', 1)[1].split("<<'PYEOF'\n", 1)[1].split('\nPYEOF', 1)[0]
        def answer(*args, **kwargs):
            account = kwargs['headers'].get('X-Bridge-Account', 'legacy')
            return Mock(status_code=status, headers={'X-Bridge-Served-By': served + account}, json=lambda: {})
        output = io.StringIO()
        with patch.dict(os.environ, {'DIST_URL': 'http://local', 'DIST_API_KEY': 'test', 'DIST_BRIDGE': 'dev', 'DIST_SCRIPTS': str(Path(__file__).parent)}), patch('requests.get', side_effect=states) as get, patch('requests.post', side_effect=answer) as post, contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            with self.assertRaises(SystemExit) as exit:
                exec(compile(code, '<distribution>', 'exec'), {})
        return exit.exception.code, output.getvalue(), get, post

    def test_distribution_fresh_selection(self):
        states = [response({'one': row(1), 'two': row(2), 'off': row(3, False)}) for _ in range(8)]
        code, out, get, post = self.distribution(states)
        self.assertEqual(code, 0, out)
        self.assertIn('DIST_OK:', out)
        self.assertEqual(get.call_count, 8)
        self.assertEqual(post.call_count, 8)

    def test_distribution_fetch_failure_and_capacity(self):
        code, out, _, post = self.distribution([requests.ConnectionError()])
        self.assertEqual(code, 1)
        post.assert_not_called()
        code, out, _, _ = self.distribution([response() for _ in range(8)], status=429)
        self.assertEqual(code, 0, out)
        self.assertIn('DIST_SKIP:', out)
        self.assertNotIn('DIST_OK:', out)

    def test_distribution_no_eligible_and_foreign(self):
        code, out, get, post = self.distribution([response({'off': row(enabled=False)})])
        self.assertEqual(code, 0, out)
        self.assertIn('DIST_SKIP:', out)
        post.assert_not_called()
        code, out, _, _ = self.distribution([response() for _ in range(8)], served='prod/worker')
        self.assertEqual(code, 1)
        self.assertIn('DIST_FAIL:', out)


if __name__ == '__main__':
    unittest.main()
