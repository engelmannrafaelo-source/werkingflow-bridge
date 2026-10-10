"""Probe for the deploy gate in front of a platform-api recreation (BR8).

Runs INSIDE a worker container of the bridge being deployed:
``docker exec -i <worker> python3 - < scripts/platform_api_job_gate.py``.
Standard library only, because the worker still runs the OLD image at that
point and cannot import anything this release adds. Everything it needs is in
the worker's own environment: its origin (BRIDGE_ORIGIN_ID), its platform-api
(PLATFORM_API_URL, BRIDGE_SERVICE_TOKEN) and its peers (FEDERATION_PEERS).

It asks two questions, both read-only:

1. The LOCAL platform-api: which jobs in this bridge's store are active? They
   run on this bridge's workers and keep their store rows through the local
   platform-api.
2. Every PEER platform-api: which jobs there have THIS bridge as budget home?
   They run on the peer's workers and ask this bridge's platform-api for pin,
   identity and budget (ADR-0011). That was the job that died on 10.10.2026
   04:09:19Z: dev origin, prod worker, dev platform-api being recreated (BR7).

Prints one JSON line per target and a summary line. Exit code:
  0  every target answered: nothing active
  1  at least one active job (busy, so wait)
  2  at least one target could not be asked (not provable, so refuse)
  3  no active job, but at least one target lacks the endpoint (HTTP 404, an
     image from before BR8). Only an explicit introduction switch may accept
     this, because the gate cannot see that target at all.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PATH = "/v1/internal/jobs-maintenance/active"
TIMEOUT_S = 5.0


def _ask(base_url: str, token: str, origin: str | None) -> dict:
    query = "?" + urllib.parse.urlencode({"origin": origin}) if origin else ""
    request = urllib.request.Request(
        base_url.rstrip("/") + PATH + query,
        headers={"X-Bridge-Service-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = json.load(response)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"state": "missing", "detail": "HTTP 404 (endpoint not deployed)"}
        return {"state": "error", "detail": f"HTTP {e.code}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return {"state": "error", "detail": f"{type(e).__name__}: {e}"}
    if not isinstance(body, dict) or not isinstance(body.get("active"), int):
        return {"state": "error", "detail": "answer without an 'active' count"}
    jobs = [
        f"{j.get('job_id')}({j.get('kind')},{j.get('status')},origin={j.get('origin')})"
        for j in body.get("jobs") or []
        if isinstance(j, dict)
    ]
    return {
        "state": "busy" if body["active"] > 0 else "idle",
        "active": body["active"],
        "waiting": body.get("waiting"),
        "jobs": jobs[: body["active"]],
    }


def main() -> int:
    own = os.getenv("BRIDGE_ORIGIN_ID", "").strip().lower()
    token = os.getenv("BRIDGE_SERVICE_TOKEN", "")
    if not own or not token:
        print(json.dumps({
            "target": "self", "state": "error",
            "detail": "BRIDGE_ORIGIN_ID or BRIDGE_SERVICE_TOKEN not set in this container",
        }))
        return 2

    targets = [(
        "local",
        os.getenv("PLATFORM_API_URL", "http://platform-api:8000"),
        token,
        None,
    )]
    try:
        peers = json.loads(os.getenv("FEDERATION_PEERS", "").strip() or "{}")
    except ValueError as e:
        print(json.dumps({"target": "peers", "state": "error",
                          "detail": f"FEDERATION_PEERS is not JSON: {e}"}))
        return 2
    if not isinstance(peers, dict):
        print(json.dumps({"target": "peers", "state": "error",
                          "detail": "FEDERATION_PEERS is not a JSON object"}))
        return 2
    for name, peer in sorted(peers.items()):
        url = peer.get("platformUrl") if isinstance(peer, dict) else None
        peer_token = os.getenv(peer.get("tokenEnv") or "", "") if isinstance(peer, dict) else ""
        if not url or not peer_token:
            print(json.dumps({"target": f"peer:{name}", "state": "error",
                              "detail": "platformUrl or token missing"}))
            return 2
        targets.append((f"peer:{name}", url, peer_token, own))

    states = []
    for label, url, tok, origin in targets:
        result = _ask(url, tok, origin)
        result["target"] = label
        if origin:
            result["origin"] = origin
        print(json.dumps(result, sort_keys=True))
        states.append(result["state"])

    print(json.dumps({"summary": True, "own_origin": own, "states": states}))
    if "error" in states:
        return 2
    if "busy" in states:
        return 1
    if "missing" in states:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
