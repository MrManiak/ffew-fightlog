"""A tiny stand-in for a Discord webhook: records every POST to a JSON-lines file.

python e2e/mock_discord.py 8765 e2e/artifacts/discord.jsonl
"""

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OUT = Path(sys.argv[2] if len(sys.argv) > 2 else "e2e/artifacts/discord.jsonl")


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - http.server API
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8")
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"path": self.path, "body": json.loads(body or "{}")}) + "\n")
        payload = json.dumps({"id": "1234567890"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 8765), Handler).serve_forever()
