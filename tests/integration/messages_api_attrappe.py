"""Attrappe der Messages-API fuer die echte CLI (CACHEFIX, 11.10.2026). Kein KI-Aufruf.

Modus "regel": die ttl-Reihenfolgeregel der echten API. Geprueft wird wie dort in der
Reihenfolge tools, system, messages; ein Block ohne ttl gilt als 5m. Kommt ein 1h-Block
nach einem 5m-Block -> 400 invalid_request_error mit dem Wortlaut aus DBD2, sonst eine
normale SSE-Antwort "OK". Modus "immer400": jede Anfrage 400 mit demselben Wortlaut.

Jede Anfrage liegt danach als req-NNN.json im Mitschnittordner, das Urteil daneben.
Aufruf als Skript: python3 -I messages_api_attrappe.py <port> <ordner> [regel|immer400]
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def ttl_violation(req):
    seen_5m = False

    def walk(path, blocks):
        nonlocal seen_5m
        if not isinstance(blocks, list):
            return None
        for i, b in enumerate(blocks):
            cc = b.get("cache_control") if isinstance(b, dict) else None
            if not cc:
                continue
            ttl = cc.get("ttl", "5m")
            if ttl == "1h" and seen_5m:
                return f"{path}.{i}.cache_control.ttl"
            if ttl == "5m":
                seen_5m = True
        return None

    for path, blocks in [("tools", req.get("tools")), ("system", req.get("system"))]:
        hit = walk(path, blocks)
        if hit:
            return hit
    for mi, m in enumerate(req.get("messages") or []):
        hit = walk(f"messages.{mi}.content", m.get("content"))
        if hit:
            return hit
    return None


def sse(ev, data):
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()


def handler(out: str, mode: str):
    """Request-Handler; ``out`` Mitschnittordner, ``mode`` "regel" oder "immer400"."""
    zaehler = [0]

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            zaehler[0] += 1
            n = zaehler[0]
            with open(f"{out}/req-{n:03d}.json", "wb") as f:
                f.write(body)
            if "count_tokens" in self.path:
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"input_tokens": 10}')
                return
            bad = (
                ttl_violation(json.loads(body))
                if mode == "regel"
                else "messages.0.content.0.cache_control.ttl"
            )
            if bad:
                err = {
                    "type": "error",
                    "error": {
                        "type": "invalid_request_error",
                        "message": f"{bad}: a ttl='1h' cache_control block must not "
                        "come after a ttl='5m' cache_control block",
                    },
                    "request_id": "req_attrappe",
                }
                data = json.dumps(err).encode()
                self.send_response(400)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                with open(f"{out}/req-{n:03d}.verdict", "w") as f:
                    f.write("400 " + bad)
                return
            with open(f"{out}/req-{n:03d}.verdict", "w") as f:
                f.write("200")
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            u = {
                "input_tokens": 5,
                "output_tokens": 1,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            msg = {
                "id": "msg_attrappe",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-5-5",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": u,
            }
            for ev, d in [
                ("message_start", {"type": "message_start", "message": msg}),
                (
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": 0,
                        "content_block": {"type": "text", "text": ""},
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "OK"},
                    },
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                        "usage": {"output_tokens": 1},
                    },
                ),
                ("message_stop", {"type": "message_stop"}),
            ]:
                self.wfile.write(sse(ev, d))

    return H


if __name__ == "__main__":
    port, ordner = int(sys.argv[1]), sys.argv[2]
    modus = sys.argv[3] if len(sys.argv) > 3 else "regel"
    HTTPServer(("127.0.0.1", port), handler(ordner, modus)).serve_forever()
