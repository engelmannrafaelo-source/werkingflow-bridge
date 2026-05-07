#!/usr/bin/env python3
"""Mock HTTP worker for nginx failover tests. STATUS_CODE env controls response."""
import os, json, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

STATUS_CODE = int(os.environ.get("STATUS_CODE", "200"))
SLEEP_SECONDS = float(os.environ.get("SLEEP_SECONDS", "0"))

class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
        if SLEEP_SECONDS > 0:
            time.sleep(SLEEP_SECONDS)
        if STATUS_CODE == 200:
            body = json.dumps({
                "id": "chatcmpl-test",
                "choices": [{"message": {"content": "ok", "role": "assistant"}, "finish_reason": "stop"}]
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(STATUS_CODE)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()

    def do_GET(self):
        body = b'{"status": "ok"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args): pass

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

if __name__ == "__main__":
    ThreadedHTTPServer(("0.0.0.0", 8000), MockHandler).serve_forever()
