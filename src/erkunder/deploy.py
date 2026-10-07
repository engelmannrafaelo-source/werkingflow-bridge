"""Fail-closed deploy admission probe, executed inside the Leitstand container."""

import json
import os
import sys
import time
import urllib.error
import urllib.request

from src.erkunder.gesundheit import PORTS


def wait_until_idle(timeout_s: int, *, poll_s: float = 5) -> None:
    url = f"http://localhost:{PORTS['leitstand']}/deploy/pruefen"
    headers = {"X-Erkunder-Intern": os.environ["ERKUNDER_INTERNAL_TOKEN"]}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method: str) -> dict:
        req = urllib.request.Request(url, method=method, headers=headers)
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    deadline = time.monotonic() + timeout_s
    ready = False
    try:
        while True:
            state = request("POST")
            reports = state["berichte"]
            if not isinstance(reports, list) or type(state["bereit"]) is not bool:
                raise ValueError("ungueltige Deploy-Antwort")
            if state["bereit"] and reports == []:
                ready = True
                print("Erkunder idle; new report admission closed", flush=True)
                return
            if state["bereit"] or not reports:
                raise ValueError("widerspruechliche Deploy-Antwort")
            print(f"Erkunder waiting for reports: {reports}", flush=True)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Erkunder drain deadline exceeded; nothing stopped")
            time.sleep(min(poll_s, remaining))
    finally:
        if not ready:
            # Also undo a successful POST whose response was lost in transport.
            # Cleanup failure stays loud; never claim admission was reopened.
            try:
                if request("DELETE") != {"freigegeben": True}:
                    raise ValueError("ungueltige Freigabe-Antwort")
            except Exception as error:
                print(
                    "Erkunder admission release FAILED: " + type(error).__name__,
                    file=sys.stderr,
                )
                raise


if __name__ == "__main__":
    try:
        wait_until_idle(int(sys.argv[1]))
    except Exception as error:
        print("Erkunder deploy gate FAIL: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
