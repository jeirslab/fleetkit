#!/usr/bin/env python3
"""Tests tools/github-app-bootstrap against a fake GitHub API and a fake sops.
No network beyond 127.0.0.1, no real sops, no secrets. Prints one JSON line
{"github_app_bootstrap":"pass"|"fail"}; exit 0 iff pass."""
import html
import http.server
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(ROOT, "tools", "github-app-bootstrap")
PEM = "FAKE-PEM-LINE-ONE\nTESTKEYMATERIAL\nFAKE-PEM-LAST-LINE\n"  # stand-in; the tool does not parse the key
fails = []


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"FAIL: {msg}", file=sys.stderr)


class Api(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        Api.seen.append(self.path)
        m = re.fullmatch(r"/app-manifests/([^/]+)/conversions", self.path)
        if m and m.group(1) == "goodcode":
            body = json.dumps({"id": 4242, "slug": "fleetkit-test", "pem": PEM,
                               "webhook_secret": "WHSECRET", "client_secret": "CLSECRET"}).encode()
            self.send_response(201)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(tmp, manifest, sops_file, code="goodcode", bad_state=False, api_port=0, preexisting=None):
    mf = os.path.join(tmp, "m.json")
    with open(mf, "w") as f:
        json.dump(manifest, f)
    if preexisting is not None:
        with open(sops_file, "w") as f:
            f.write(preexisting)
    p = subprocess.Popen(
        [sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sops_file,
         "--sops", os.path.join(tmp, "sops"), "--api", f"http://127.0.0.1:{api_port}",
         "--no-browser", "--timeout", "20"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first = p.stderr.readline()
    url = re.search(r"Open (\S+) ", first).group(1)
    page = urllib.request.urlopen(url).read().decode()
    form_action = html.unescape(re.search(r'action="([^"]+)"', page).group(1))
    posted = json.loads(html.unescape(re.search(r'name=manifest value="([^"]*)"', page).group(1)))
    state = re.search(r"state=([^&]+)$", form_action).group(1)
    check(form_action.startswith("https://github.com/organizations/acme/settings/apps/new?state="),
          "form targets the org registration URL")
    check(posted["redirect_url"] == url + "callback", "redirect_url points at the local callback")
    cb = url + f"callback?code={code}&state={'wrong' if bad_state else state}"
    try:
        urllib.request.urlopen(cb)
        if bad_state:
            check(False, "wrong state must be rejected")
    except urllib.error.HTTPError as e:
        check(bad_state and e.code == 400, "wrong state gives 400")
    if bad_state:
        urllib.request.urlopen(url + f"callback?code={code}&state={state}")
    out, err = p.communicate(timeout=30)
    return p.returncode, first + err, out


def main():
    manifest = {"name": "fleetkit-acme", "url": "https://example.com",
                "default_permissions": {"contents": "read", "administration": "write"},
                "default_events": [], "public": False}
    srv = http.server.HTTPServer(("127.0.0.1", 0), Api)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_port
    with tempfile.TemporaryDirectory() as tmp:
        sops = os.path.join(tmp, "sops")
        with open(sops, "w") as f:
            f.write('#!/bin/sh\n[ "$1" = edit ] || exit 9\nt=$(mktemp)\n[ -f "$2" ] && cp "$2" "$t"\n'
                    '$EDITOR "$t" || exit 1\ncp "$t" "$2"\nrm -f "$t"\n')
        os.chmod(sops, stat.S_IRWXU)

        # fresh file
        sf = os.path.join(tmp, "new.yaml")
        rc, err, out = run(tmp, manifest, sf, api_port=port)
        check(rc == 0, f"fresh run exits 0 (got {rc}): {err}")
        body = open(sf).read()
        check('github_app_id: "4242"' in body, "id stored")
        check("github_app_private_key: |\n  FAKE-PEM-LINE-ONE\n  TESTKEYMATERIAL" in body, "pem stored as block scalar")
        for leak in ("TESTKEYMATERIAL", "4242", "WHSECRET", "CLSECRET"):
            check(leak not in out and leak not in err, f"{leak} not printed")
        check("WHSECRET" not in body and "CLSECRET" not in body, "webhook/client secrets discarded")

        # existing file keeps other keys, replaces ours
        sf2 = os.path.join(tmp, "old.yaml")
        rc, err, _ = run(tmp, manifest, sf2, api_port=port,
                         preexisting='other: keep\ngithub_app_id: "1"\ngithub_app_private_key: |\n  old\n  older\nlast: x\n')
        body = open(sf2).read()
        check(rc == 0, f"existing run exits 0: {err}")
        check("other: keep" in body and "last: x" in body, "other keys kept")
        check("old" not in body and body.count("github_app_id") == 1 and '"4242"' in body, "old values replaced")

        # JSON file
        sf3 = os.path.join(tmp, "j.json")
        rc, err, _ = run(tmp, manifest, sf3, api_port=port, preexisting='{"a": 1}')
        d = json.load(open(sf3))
        check(rc == 0 and d.get("a") == 1 and d.get("github_app_id") == "4242" and d.get("github_app_private_key") == PEM,
              "json file merged")

        # state mismatch is rejected but the real callback still works
        sf4 = os.path.join(tmp, "s.yaml")
        rc, err, _ = run(tmp, manifest, sf4, bad_state=True, api_port=port)
        check(rc == 0, f"state mismatch then good callback: {err}")

        # bad code: nonzero, nothing written
        sf5 = os.path.join(tmp, "b.yaml")
        rc, err, _ = run(tmp, manifest, sf5, code="expired", api_port=port)
        check(rc != 0 and not os.path.exists(sf5), "bad code fails and writes nothing")

        # argument validation
        mf = os.path.join(tmp, "bad.json")
        json.dump({"name": "x", "url": "u", "default_permissions": {"contents": "owner"}}, open(mf, "w"))
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sf5, "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "invalid level" in r.stderr, "bad permission level refused")
        json.dump({"name": "x", "url": "u", "default_permissions": {}, "hook_attributes": {"url": "u", "active": True}}, open(mf, "w"))
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sf5, "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0, "manifest with webhook or empty permissions refused")
        r = subprocess.run([sys.executable, TOOL, "--org", "-bad org", "--manifest", mf, "--sops-file", sf5],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "invalid organization" in r.stderr, "bad org refused")
    srv.shutdown()
    status = "fail" if fails else "pass"
    print(json.dumps({"github_app_bootstrap": status}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
