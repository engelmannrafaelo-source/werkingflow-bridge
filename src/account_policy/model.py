"""Single bridge account policy. Personal CUI account selection is unrelated."""
from copy import deepcopy

SOURCE = "bridge-account-policy"
KEY = "bridge_account_policy"
# Existing deployed worker identities; unknown/new accounts are never auto-enabled.
ROSTER = {
    "sahori": "partner", "kurt": "partner", "coach": "partner", "erk": "partner",
    "engelmann": "dev", "office": "dev", "gmail": "dev", "werking": "dev",
}

def validate(value):
    if not isinstance(value, dict) or set(value) - {"revision", "accounts", "source"}:
        raise ValueError("invalid policy object")
    revision = value.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("revision must be a nonnegative integer")
    rows = value.get("accounts")
    if not isinstance(rows, list) or len(rows) != len(ROSTER):
        raise ValueError("complete existing account roster required")
    seen, ranks, result = set(), set(), []
    for row in rows:
        if not isinstance(row, dict) or set(row) != {"house", "accountId", "enabled", "priority"}:
            raise ValueError("invalid account fields")
        account = row["accountId"]
        if not isinstance(account, str) or account not in ROSTER or row["house"] != ROSTER[account]:
            raise ValueError("unknown account or wrong house")
        if account in seen:
            raise ValueError("duplicate account")
        rank = row["priority"]
        if type(row["enabled"]) is not bool or type(rank) is not int or not 1 <= rank <= len(ROSTER):
            raise ValueError("invalid enabled or priority")
        if rank in ranks:
            raise ValueError("duplicate priority")
        seen.add(account); ranks.add(rank); result.append(deepcopy(row))
    return {"source": SOURCE, "revision": revision, "accounts": sorted(result, key=lambda x: x["priority"])}

def initial_policy():
    """Migration only: preserve the eight existing automatic workers, Partner first."""
    return validate({"revision": 0, "accounts": [
        {"house": house, "accountId": account, "enabled": True, "priority": i}
        for i, (account, house) in enumerate(ROSTER.items(), 1)
    ]})

def entry(policy, account_id):
    policy = validate(policy)
    return next((x for x in policy["accounts"] if x["accountId"] == account_id), None)

def rank(policy, account_id):
    row = entry(policy, account_id)
    return row["priority"] if row and row["enabled"] else None

def choose(policy, eligible_ids, preferred_account_id=None, strict=False):
    """A preference is a hint; strict is an explicitly requested hard binding."""
    policy = validate(policy)
    eligible = set(eligible_ids)
    if preferred_account_id is not None:
        if preferred_account_id in eligible and rank(policy, preferred_account_id) is not None:
            return preferred_account_id
        if strict:
            return None
    return next((r["accountId"] for r in policy["accounts"] if r["enabled"] and r["accountId"] in eligible), None)

def overlay(policy, accounts):
    policy = validate(policy)
    result = deepcopy(accounts)
    for account, info in result.items():
        row = entry(policy, account)
        info["bridge_enabled"] = bool(row and row["enabled"])
        info["bridge_priority"] = row["priority"] if row else None
        info["bridge_policy_revision"] = policy["revision"]
    return result
