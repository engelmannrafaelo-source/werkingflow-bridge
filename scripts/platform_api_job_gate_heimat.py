"""Peer side of the platform-api deploy gate (P1, SEC 18.11.2026, Auftrag 692bcf6c).

Runs INSIDE a worker container of the PEER bridge, e.g. for a dev deploy in a
prod worker on the prod worker host:

    ssh root@<prod-worker-host> "timeout 60 docker exec -i <prod-worker> python3 - --eigen prod --heimat dev" \
        < scripts/platform_api_job_gate_heimat.py

Question: which jobs in THIS (peer) bridge's store have the bridge being
deployed as budget home (``origin=<heimat>``)? Those run on this bridge's
workers and ask the home bridge's platform-api for pin, identity and budget
(ADR-0011, BR7). It is the same question the deploying bridge used to ask with
a copy of this bridge's service token (FEDERATION_TOKEN_<PEER>). Asked from
here, the credential stays where it is: the worker's own BRIDGE_SERVICE_TOKEN,
used in this process only, never printed, never an argument.

Standard library only (the worker runs whatever image is live). Fail-closed:
  * BRIDGE_ORIGIN_ID of this container must equal ``--eigen`` exactly;
  * BRIDGE_SERVICE_TOKEN and PLATFORM_API_URL must be set (no default URL —
    a guessed URL could be some other store);
  * the answer must carry integer ``active`` and ``waiting`` >= 0.
Exit code (one JSON line per target plus a summary line on stdout):
  0  idle    (active == 0 and waiting == 0)
  1  busy    (active > 0 or waiting > 0)
  2  error   (anything else: config, HTTP, network, timeout, malformed answer)
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

PATH = "/v1/internal/jobs-maintenance/active"
TIMEOUT_S = 5.0
_ORIGIN_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


def _args(argv: list) -> dict | None:
    out: dict = {}
    i = 0
    while i < len(argv):
        key = argv[i]
        if key not in ("--eigen", "--heimat") or i + 1 >= len(argv) or key in out:
            return None
        val = argv[i + 1].strip().lower()
        if not _ORIGIN_RE.match(val):
            return None
        out[key] = val
        i += 2
    if set(out) != {"--eigen", "--heimat"} or out["--eigen"] == out["--heimat"]:
        return None
    return out


def _fehler(detail: str, eigen: str = "", heimat: str = "") -> int:
    print(json.dumps({"target": f"heimat:{eigen or '?'}", "origin": heimat or None,
                      "state": "error", "detail": detail}, sort_keys=True))
    print(json.dumps({"summary": True, "own_origin": eigen or None, "states": ["error"]}))
    return 2


def main(argv: list | None = None) -> int:
    a = _args(sys.argv[1:] if argv is None else argv)
    if a is None:
        return _fehler("bad arguments (need --eigen <origin> --heimat <origin>, different)")
    eigen, heimat = a["--eigen"], a["--heimat"]
    own = os.getenv("BRIDGE_ORIGIN_ID", "").strip().lower()
    if own != eigen:
        # The wrong bridge must never answer for the right one.
        return _fehler(f"container origin is {own or 'unset'!r}, expected {eigen!r}", eigen, heimat)
    token = os.getenv("BRIDGE_SERVICE_TOKEN", "")
    base = os.getenv("PLATFORM_API_URL", "").strip()
    if not token or not base:
        return _fehler("BRIDGE_SERVICE_TOKEN or PLATFORM_API_URL not set in this container", eigen, heimat)
    request = urllib.request.Request(
        base.rstrip("/") + PATH + "?" + urllib.parse.urlencode({"origin": heimat}),
        headers={"X-Bridge-Service-Token": token},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            body = json.load(response)
    except urllib.error.HTTPError as e:
        return _fehler(f"HTTP {e.code}", eigen, heimat)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return _fehler(type(e).__name__, eigen, heimat)
    if not isinstance(body, dict) or type(body.get("active")) is not int \
            or type(body.get("waiting")) is not int or body["active"] < 0 or body["waiting"] < 0:
        return _fehler("answer without valid 'active'/'waiting' counts", eigen, heimat)
    jobs = body.get("jobs") if isinstance(body.get("jobs"), list) else []
    state = "busy" if body["active"] > 0 or body["waiting"] > 0 else "idle"
    print(json.dumps({
        "target": f"heimat:{eigen}", "origin": heimat, "state": state,
        "active": body["active"], "waiting": body["waiting"],
        "jobs": [f"{j.get('job_id')}({j.get('kind')},{j.get('status')},origin={j.get('origin')},"
                 f"deferred_until={j.get('deferred_until')})" for j in jobs if isinstance(j, dict)][:50],
    }, sort_keys=True))
    print(json.dumps({"summary": True, "own_origin": eigen, "states": [state]}))
    return 0 if state == "idle" else 1


if __name__ == "__main__":
    sys.exit(main())
