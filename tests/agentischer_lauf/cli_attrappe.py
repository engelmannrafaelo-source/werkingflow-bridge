"""Messages-API-Attrappe fuer die echte CLI im CLI-Platz (kein KI-Aufruf). argv: szenario port logdatei

werkzeug: Read ../eingang/notiz.txt -> Write ergebnis.txt -> Text end_turn
grenze:   jede Antwort meldet input_tokens=400 und ruft wieder Read auf (Endlosschleife)
ausgabe:  message_delta meldet output_tokens=999 (ueber max_tokens)
ueberlast: HTTP 529 overloaded_error
settings:<datei>:<inhalt>: Write <datei>, dann zweimal Read, dann end_turn
count_tokens antwortet immer {"input_tokens": 7}.
"""
import json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SZ, PORT, LOG = sys.argv[1], int(sys.argv[2]), sys.argv[3]
state = {"n": 0}


def sse(ev, data):
    return f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _chunk(self, b):
        self.wfile.write(f"{len(b):x}\r\n".encode() + b + b"\r\n")
        self.wfile.flush()

    def _json(self, code, obj):
        out = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):
        self._json(404, {})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        if "count_tokens" in self.path:
            with open(LOG, "a") as f:
                f.write(json.dumps({"count_tokens": True, "auth": bool(self.headers.get("x-api-key") or self.headers.get("authorization"))}) + "\n")
            return self._json(200, {"input_tokens": 7})
        state["n"] += 1
        n = state["n"]
        tools = [t.get("name") for t in body.get("tools") or []]
        with open(LOG, "a") as f:
            f.write(json.dumps({"n": n, "path": self.path, "max_tokens": body.get("max_tokens"), "tools": tools}) + "\n")
        if SZ == "ueberlast":
            return self._json(529, {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}})
        msgs = body.get("messages") or []
        tool_results = sum(1 for m in msgs for b in (m.get("content") if isinstance(m.get("content"), list) else []) if isinstance(b, dict) and b.get("type") == "tool_result")
        inp, out, stop = 10, 20, "end_turn"
        block = {"type": "text", "text": "fertig"}
        if SZ == "werkzeug" and tool_results == 0:
            block, stop = {"type": "tool_use", "id": f"toolu_{n}", "name": "Read", "input": {"file_path": "../eingang/notiz.txt"}}, "tool_use"
        elif SZ == "werkzeug" and tool_results == 1:
            block, stop = {"type": "tool_use", "id": f"toolu_{n}", "name": "Write", "input": {"file_path": "ergebnis.txt", "content": "Ergebnis aus der Notiz\n"}}, "tool_use"
        elif SZ == "grenze":
            inp = 400
            block, stop = {"type": "tool_use", "id": f"toolu_{n}", "name": "Read", "input": {"file_path": "../eingang/notiz.txt"}}, "tool_use"
        elif SZ.startswith("settings:"):
            # settings:<datei>:<json>  turn 1 writes the project settings file,
            # turns 2-3 call Read, then end_turn.
            _, datei, inhalt = SZ.split(":", 2)
            if tool_results == 0:
                block, stop = {"type": "tool_use", "id": f"toolu_{n}", "name": "Write", "input": {"file_path": datei, "content": inhalt}}, "tool_use"
            elif tool_results in (1, 2):
                block, stop = {"type": "tool_use", "id": f"toolu_{n}", "name": "Read", "input": {"file_path": "../eingang/notiz.txt"}}, "tool_use"
        elif SZ == "ausgabe":
            out = 999
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("request-id", f"req_{n}")
        self.end_headers()
        self._chunk(sse("message_start", {"type": "message_start", "message": {"id": f"msg_{n}", "type": "message", "role": "assistant", "model": body.get("model"), "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": inp, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 1}}}))
        if block["type"] == "text":
            self._chunk(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}))
            self._chunk(sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": block["text"]}}))
        else:
            self._chunk(sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}))
            self._chunk(sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}}))
        self._chunk(sse("content_block_stop", {"type": "content_block_stop", "index": 0}))
        self._chunk(sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": out}}))
        self._chunk(sse("message_stop", {"type": "message_stop"}))
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
