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
  4  everything asked from here is idle, but at least one peer was named with
     ``--peer-remote`` and is NOT asked from here (P1, see below). This is NOT
     a release: the caller must ask that peer's store from the peer's own side
     (scripts/platform_api_job_gate_heimat.py) and only an idle answer there
     completes the proof.

"busy" means active > 0 OR waiting > 0 (SEC 18.11.2026): a parked job with a
running wait still depends on its home platform-api the moment the wait ends.

--peer-remote NAME[,NAME] (P1, SEC 18.11.2026): the named peers are asked from
their own side with their own credential, so no peer credential has to live on
this bridge. Every named peer must exist in FEDERATION_PEERS (exact, else rc 2);
peers NOT named are asked from here exactly as before. Naming a peer never
releases anything by itself — it turns the answer into rc 4 at best.
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
    if not isinstance(body, dict) or type(body.get("active")) is not int \
            or type(body.get("waiting")) is not int or body["active"] < 0 or body["waiting"] < 0:
        return {"state": "error", "detail": "answer without valid 'active'/'waiting' counts"}
    jobs = [
        f"{j.get('job_id')}({j.get('kind')},{j.get('status')},origin={j.get('origin')})"
        for j in body.get("jobs") or []
        if isinstance(j, dict)
    ]
    return {
        "state": "busy" if body["active"] > 0 or body["waiting"] > 0 else "idle",
        "active": body["active"],
        "waiting": body.get("waiting"),
        "jobs": jobs[: body["active"] + body["waiting"]],
    }


def _peer_remote(argv: list) -> set | None:
    """Parse --peer-remote NAME[,NAME]. None = malformed arguments."""
    remote: set = set()
    i = 0
    while i < len(argv):
        if argv[i] != "--peer-remote" or i + 1 >= len(argv):
            return None
        names = [n.strip().lower() for n in argv[i + 1].split(",")]
        if not all(names):
            return None
        remote.update(names)
        i += 2
    return remote


def main(argv: list | None = None) -> int:
    remote = _peer_remote(sys.argv[1:] if argv is None else argv)
    if remote is None:
        print(json.dumps({"target": "self", "state": "error", "detail": "bad arguments"}))
        return 2
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
    peers = {str(k).strip().lower(): v for k, v in peers.items()}
    unknown = sorted(remote - set(peers))
    if unknown:
        print(json.dumps({"target": "peers", "state": "error",
                          "detail": f"--peer-remote names no configured peer: {unknown}"}))
        return 2
    remote_lines = []
    for name, peer in sorted(peers.items()):
        if name in remote:
            # Asked from the peer's own side (P1). Not looked at here at all:
            # neither its URL nor its token (which may be absent on purpose).
            remote_lines.append({"target": f"peer:{name}", "state": "remote", "origin": own,
                                 "detail": "asked from the peer's own side (platform_api_job_gate_heimat.py)"})
            continue
        url = peer.get("platformUrl") if isinstance(peer, dict) else None
        peer_token = os.getenv(peer.get("tokenEnv") or "", "") if isinstance(peer, dict) else ""
        if not url or not peer_token:
            print(json.dumps({"target": f"peer:{name}", "state": "error",
                              "detail": "platformUrl or token missing"}))
            return 2
        targets.append((f"peer:{name}", url, peer_token, own))

    states = []
    local_url = targets[0][1].rstrip("/")
    local_result = None
    for label, url, tok, origin in targets:
        if label != "local" and url.rstrip("/") == local_url:
            # The unfiltered local probe already covers ALL origins in this
            # exact store. Reuse every verdict (including errors/missing), not
            # a second authentication attempt with a regression peer's token.
            result = dict(local_result, covered_by="local/all_origins")
        else:
            result = _ask(url, tok, origin)
        if label == "local":
            local_result = dict(result)
        result["target"] = label
        if origin:
            result["origin"] = origin
        print(json.dumps(result, sort_keys=True))
        states.append(result["state"])

    for line in remote_lines:
        print(json.dumps(line, sort_keys=True))
        states.append(line["state"])
    print(json.dumps({"summary": True, "own_origin": own, "states": states}))
    if "error" in states:
        return 2
    if "busy" in states:
        return 1
    if "missing" in states:
        return 3
    if "remote" in states:
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
