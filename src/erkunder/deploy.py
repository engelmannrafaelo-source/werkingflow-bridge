"""Fail-closed deploy admission probe, executed inside the Leitstand container."""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


class LegacyProtocolMissing(RuntimeError):
    """Only an authenticated POST returning 404 identifies the old protocol."""


def assert_legacy_idle(root: Path = Path("/arbeit")) -> None:
    # The old API has no aggregate running-report endpoint. Require an empty
    # volume, including hidden entries; unknown or historical data fails closed.
    entries = list(root.iterdir())
    if entries:
        print(
            f"ERKUNDER-EINFUEHRUNG refused: /arbeit has {len(entries)} entries",
            flush=True,
        )
        raise RuntimeError(
            f"ERKUNDER-EINFUEHRUNG: /arbeit not empty ({len(entries)} entries)"
        )
    print("ERKUNDER-EINFUEHRUNG: /arbeit empty, 0 reports", flush=True)


def wait_until_idle(
    timeout_s: int, *, poll_s: float = 5, port: int | None = None
) -> None:
    if port is None:
        from src.erkunder.gesundheit import PORTS

        port = PORTS["leitstand"]
    url = f"http://localhost:{port}/deploy/pruefen"
    headers = {"X-Erkunder-Intern": os.environ["ERKUNDER_INTERNAL_TOKEN"]}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(method: str) -> dict:
        req = urllib.request.Request(url, method=method, headers=headers)
        with opener.open(req, timeout=5) as response:
            return json.load(response)

    deadline = time.monotonic() + timeout_s
    ready = False
    legacy = False
    try:
        while True:
            try:
                state = request("POST")
            except urllib.error.HTTPError as error:
                if error.code != 404:
                    raise
                legacy = True
                raise LegacyProtocolMissing(
                    "POST /deploy/pruefen -> 404; introduction requires "
                    "ERKUNDER_DEPLOY_EINFUEHRUNG=1 and empty /arbeit"
                ) from None
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
        if not ready and not legacy:
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
        if sys.argv[1] == "--legacy-idle":
            assert_legacy_idle()
        else:
            # The deploy tool supplies the port from its SSoT. No new module is
            # imported inside an old image that predates gesundheit.py.
            wait_until_idle(int(sys.argv[1]), port=int(sys.argv[2]))
    except LegacyProtocolMissing as error:
        print("Erkunder deploy gate FAIL: " + str(error), file=sys.stderr)
        sys.exit(3)
    except Exception as error:
        print("Erkunder deploy gate FAIL: " + type(error).__name__, file=sys.stderr)
        sys.exit(1)
