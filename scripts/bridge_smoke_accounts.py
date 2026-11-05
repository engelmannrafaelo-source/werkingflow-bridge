"""Fresh local account preselection for deploy probes, never an admission bypass."""
import math
import requests


class PoolStateError(ValueError):
    pass


def local_accounts(base_url, headers, min_tokens=100):
    """Return ranked eligible local IDs, or None for a pre-policy bootstrap.

    The local metrics reader enumerates this bridge's configured workers only.
    Request failure or partial/malformed policy is NOT a legacy bootstrap.
    """
    try:
        response = requests.get(base_url.rstrip('/') + '/v1/metrics/account-pool-state',
                                headers=headers, timeout=20, allow_redirects=False)
        response.raise_for_status()
        state = response.json()
        accounts = state['accounts']
        if not isinstance(accounts, dict) or not all(isinstance(a, dict) for a in accounts.values()):
            raise ValueError('invalid accounts')
        fields = {'bridge_enabled', 'bridge_priority', 'bridge_policy_revision'}
        if 'bridge_policy_revision' not in state and not any(fields.intersection(a) for a in accounts.values()):
            return None
        revision = state.get('bridge_policy_revision')
        if type(revision) is not int or revision < 0:
            raise ValueError('invalid policy revision')
        eligible = []
        for name, a in accounts.items():
            rank = a.get('bridge_priority')
            if (type(a.get('bridge_enabled')) is not bool or type(rank) is not int or rank < 1
                    or type(a.get('bridge_policy_revision')) is not int
                    or a['bridge_policy_revision'] != revision):
                raise ValueError('incomplete account policy')
            def number(key):
                value = float(a.get(key) or 0)
                if not math.isfinite(value):
                    raise ValueError('nonfinite capacity')
                return value
            if (a['bridge_enabled'] and a.get('available') is True
                    and number('cooldown_remaining_s') == 0
                    and number('effective_cap_tokens') - number('current_in_flight_tokens') > min_tokens
                    and not (a.get('usage_known') is True and number('weekly_percent') >= 96)):
                eligible.append((rank, name))
        return [name for _, name in sorted(eligible)]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        raise PoolStateError('local account policy/capacity unavailable: ' + type(exc).__name__) from exc
