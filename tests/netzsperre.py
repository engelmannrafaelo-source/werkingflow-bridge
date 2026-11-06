"""Network lock for tests of paths that talk to third parties.

Import this module FIRST — before any ``src`` import — in a test module whose
subject could reach the network through a transport the test did not
replace (Messages API, Perplexity, platform-api, the privacy service). It
makes every socket connect and name lookup of the process raise
``NetzGesperrt`` with the target, so a leak is a loud failure, never a
silent real request (KD 27.10.2026: a probe with two parallel handler calls
sent a real request to api.anthropic.com).

Usage in a test module::

    import tests.netzsperre as netzsperre   # first line: lock is active
    ... src imports ...
    netzsperre.aufheben()                   # collection done: other modules
                                            # of the same process keep their
                                            # real (failing) sockets

    @pytest.fixture(scope="module", autouse=True)
    def _netzsperre():
        netzsperre.aktivieren()
        yield
        netzsperre.aufheben()

So the lock holds while this module's src imports run and while its tests
run — and nowhere else in the process.
"""
from __future__ import annotations

import socket


class NetzGesperrt(RuntimeError):
    """A test tried to open a network connection."""


def _ziel(args) -> str:
    for a in args:
        if isinstance(a, (tuple, list)) and len(a) >= 2:
            return f"{a[0]}:{a[1]}"
        if isinstance(a, (str, bytes)):
            return str(a)
    return repr(args)


def _gesperrt(was):
    def _sperre(*args, **kwargs):
        raise NetzGesperrt(f"Netzsperre: {was} -> {_ziel(args[1:] if args and isinstance(args[0], socket.socket) else args)}")
    return _sperre


_ORIGINAL = {
    "connect": socket.socket.connect,
    "connect_ex": socket.socket.connect_ex,
    "create_connection": socket.create_connection,
    "getaddrinfo": socket.getaddrinfo,
    "gethostbyname": socket.gethostbyname,
}
AKTIV = False


def aktivieren() -> None:
    """Install the lock (idempotent)."""
    global AKTIV
    socket.socket.connect = _gesperrt("socket.connect")  # type: ignore[assignment]
    socket.socket.connect_ex = _gesperrt("socket.connect_ex")  # type: ignore[assignment]
    socket.create_connection = _gesperrt("socket.create_connection")  # type: ignore[assignment]
    socket.getaddrinfo = _gesperrt("socket.getaddrinfo")  # type: ignore[assignment]
    socket.gethostbyname = _gesperrt("socket.gethostbyname")  # type: ignore[assignment]
    AKTIV = True


def aufheben() -> None:
    """Restore the real functions — for the other modules of the same
    process, whose tests rely on real (failing) connections."""
    global AKTIV
    socket.socket.connect = _ORIGINAL["connect"]  # type: ignore[assignment]
    socket.socket.connect_ex = _ORIGINAL["connect_ex"]  # type: ignore[assignment]
    socket.create_connection = _ORIGINAL["create_connection"]  # type: ignore[assignment]
    socket.getaddrinfo = _ORIGINAL["getaddrinfo"]  # type: ignore[assignment]
    socket.gethostbyname = _ORIGINAL["gethostbyname"]  # type: ignore[assignment]
    AKTIV = False


# Active from the moment of import: a test module imports this FIRST, so
# every src import that follows happens under the lock.
aktivieren()
