# Fake huggingface.co for the gate's offline smoke test. Serves one file,
# fake/model/weights.bin, with the headers Olah expects of a real hub.
import hashlib
import http.server
import json
import sys

PORT, LOG, WEIGHTS = int(sys.argv[1]), sys.argv[2], sys.argv[3]
DATA = open(WEIGHTS, "rb").read()
SHA = hashlib.sha256(DATA).hexdigest()
COMMIT = "0" * 40


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        with open(LOG, "a") as f:
            f.write(self.command + " " + self.path + " " + (self.headers.get("Range") or "") + "\n")

    def _send(self, code, body=b"", headers=None, head=False):
        self.send_response(code)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _route(self, head):
        p = self.path.split("?")[0]
        if p.startswith("/api/models/fake/model/tree/"):
            body = json.dumps([{"type": "file", "path": "weights.bin", "size": len(DATA), "lfs": {"size": len(DATA)}},
                               {"type": "file", "path": "skip/other.txt", "size": 1}]).encode()
            return self._send(200, body, {"Content-Type": "application/json"}, head)
        if p.startswith("/api/models/fake/model"):
            body = json.dumps({"id": "fake/model", "sha": COMMIT, "siblings": [{"rfilename": "weights.bin"}]}).encode()
            return self._send(200, body, {"Content-Type": "application/json"}, head)
        if p.endswith("/weights.bin") and "/resolve/" in p:
            rng = self.headers.get("Range")
            body = DATA
            code = 200
            hdrs = {"Content-Type": "application/octet-stream", "ETag": f'"{SHA}"',
                    "X-Repo-Commit": COMMIT, "X-Linked-Etag": f'"{SHA}"',
                    "X-Linked-Size": str(len(DATA)), "Accept-Ranges": "bytes"}
            if rng and rng.startswith("bytes="):
                a, _, b = rng[6:].partition("-")
                a = int(a or 0)
                b = int(b) if b else len(DATA) - 1
                body = DATA[a:b + 1]
                code = 206
                hdrs["Content-Range"] = f"bytes {a}-{b}/{len(DATA)}"
            return self._send(code, body, hdrs, head)
        return self._send(404, b"{}", {"Content-Type": "application/json"}, head)

    def do_GET(self):
        self._route(False)

    def do_POST(self):
        # paths-info: Olah asks about the file before serving it.
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps([{"path": "weights.bin", "type": "file", "size": len(DATA), "oid": SHA,
                            "lfs": {"oid": SHA, "size": len(DATA), "pointerSize": 134}}]).encode()
        self._send(200, body, {"Content-Type": "application/json"})

    def do_HEAD(self):
        self._route(True)


http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
