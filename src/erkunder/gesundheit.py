"""Container readiness probes; no model calls, report writes or external traffic."""

import json
import os
import socket
import sys
import urllib.error
import urllib.request

PORTS = {"leitstand": 8100, "platz": 8200}


def probe(role: str) -> None:
    if role == "proxy":
        # A rejected CONNECT proves the HTTP proxy and its deny policy work.
        with socket.create_connection(("localhost", 8888), timeout=2) as connection:
            connection.sendall(b"CONNECT forbidden.invalid:443 HTTP/1.0\r\n\r\n")
            response = connection.makefile("rb").readline(1024)
        if response.split()[1:2] != [b"403"]:
            raise RuntimeError("Erkunder-Ausgang verweigert Probe nicht mit 403")
        return
    port = PORTS[role]
    # Docker healthcheck gets the configured env, even though the protected
    # place server removes its copy at startup. Never print the token.
    token = os.environ["ERKUNDER_INTERNAL_TOKEN"]
    request = urllib.request.Request(
        f"http://localhost:{port}/__bereitschaft__",
        headers={"X-Erkunder-Intern": token},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(request, timeout=2).close()
    except urllib.error.HTTPError as error:
        # Authenticated missing route: middleware and completed lifespan work.
        if error.code == 404:
            return
        detail = ""
        if error.code == 503 and role == "platz":
            # Only known service reasons, never arbitrary response text/secrets.
            try:
                body = json.loads(error.read(1024))
                if body.get("detail") == "Platz-Aufraeumen fehlgeschlagen":
                    detail = ": Platz-Aufraeumen fehlgeschlagen"
            except (ValueError, AttributeError):
                pass  # HTTP status remains a loud failure even without JSON.
        raise RuntimeError(f"Erkunder-{role}: HTTP {error.code}{detail}") from None
    raise RuntimeError(f"Erkunder-{role}: unerwartete Bereitschaftsantwort")


if __name__ == "__main__":
    probe(sys.argv[1])
