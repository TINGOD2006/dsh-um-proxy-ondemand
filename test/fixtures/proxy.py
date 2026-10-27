"""Isolated lifecycle fixture: no credentials, browser, logs, or gateway access."""
import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, required=True)
parser.add_argument("--log-file", default="")
args, _ = parser.parse_known_args()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        try:
            if "stall" in args.log_file:
                self.wfile.write(b"{")
                self.wfile.flush()
                while True:
                    time.sleep(0.02)
                    self.wfile.write(b" ")
                    self.wfile.flush()
            else:
                self.wfile.write(json.dumps({"service": "um-opencode-proxy"}).encode())
        except OSError:
            pass

    def log_message(self, *_):
        pass


ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()
