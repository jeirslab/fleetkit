#!/usr/bin/env python3
"""Tests tools/github-app-bootstrap against a fake GitHub API and a fake sops.
No network beyond 127.0.0.1, no real sops, no secrets. Prints one JSON line
{"github_app_bootstrap":"pass"|"fail"}; exit 0 iff pass."""
import base64
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

FAKE_SOPS = r'''#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
secrets = ("TESTKEYMATERIAL", "WHSECRET", "CLSECRET")
def violate(what):
    open(os.environ["FAKE_SOPS_LOG"], "a").write(what + "\\n")
if any(s in x for x in a for s in secrets) or any(s in v for v in os.environ.values() for s in secrets):
    violate("secret in argv/env")
if a[:2] == ["set", "--value-stdin"]:
    f, path = a[2], a[3]
    if not os.path.isfile(f):
        sys.exit(1)
    d = json.load(open(f))
    d[path] = json.loads(sys.stdin.read())
    json.dump(d, open(f, "w"))
elif a[:2] == ["decrypt", "--extract"]:
    sys.stdout.write(json.load(open(a[3]))[a[2]])
else:
    sys.exit(9)
'''
FAKE_OPENSSL = r'''#!/usr/bin/env python3
import sys
if sys.argv[1:4] != ["dgst", "-sha256", "-sign"] or sys.argv[4] != "/dev/stdin":
    sys.exit(9)
if sys.stdin.read() != @PEM@:
    sys.exit(1)
sys.stdout.buffer.write(b"SIG")
'''


def check(cond, msg):
    if not cond:
        fails.append(msg)
        print(f"FAIL: {msg}", file=sys.stderr)


class Api(http.server.BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        Api.seen.append(self.path)
        auth = self.headers.get("Authorization", "")
        ok = False
        if self.path.startswith("/app/installations") and auth.startswith("Bearer "):
            parts = auth[7:].split(".")
            pad = lambda x: x + "=" * (-len(x) % 4)
            try:
                head = json.loads(base64.urlsafe_b64decode(pad(parts[0])))
                pay = json.loads(base64.urlsafe_b64decode(pad(parts[1])))
                ok = (len(parts) == 3 and head["alg"] == "RS256" and pay["iss"] == "4242"
                      and pay["exp"] - pay["iat"] <= 600 and base64.urlsafe_b64decode(pad(parts[2])) == b"SIG")
            except Exception:
                ok = False
        if not ok:
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        page = self.path.split("page=")[-1]
        data = [{"id": 1, "account": {"login": "other"}}, {"id": 777, "account": {"login": "Acme"}}] if page == "1" else []
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        Api.seen.append(self.path)
        m = re.fullmatch(r"/app-manifests/([^/]+)/conversions", self.path)
        if m and m.group(1) == "goodcode":
            body = json.dumps({"id": 4242, "slug": "fleetkit-test", "pem": PEM, "client_id": "Iv1.cid",
                               "html_url": "https://github.com/apps/fleetkit-test",
                               "webhook_secret": "WHSECRET", "client_secret": "CLSECRET"}).encode()
            self.send_response(201)
        else:
            body = b"{}"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(tmp, manifest, sops_file, code="goodcode", bad_state=False, api_port=0, preexisting="{}", who=("--org", "acme"), prefix="github.app"):
    mf = os.path.join(tmp, "m.json")
    with open(mf, "w") as f:
        json.dump(manifest, f)
    if preexisting is not None:
        with open(sops_file, "w") as f:
            f.write(preexisting)
    p = subprocess.Popen(
        [sys.executable, TOOL, *who, "--manifest", mf, "--sops-file", sops_file, "--key-prefix", prefix,
         "--sops", os.path.join(tmp, "sops"), "--api", f"http://127.0.0.1:{api_port}",
         "--no-browser", "--timeout", "20"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first = p.stderr.readline()
    url = re.search(r"Open (\S+) ", first).group(1)
    page = urllib.request.urlopen(url).read().decode()
    form_action = html.unescape(re.search(r'action="([^"]+)"', page).group(1))
    posted = json.loads(html.unescape(re.search(r'name=manifest value="([^"]*)"', page).group(1)))
    state = re.search(r"state=([^&]+)$", form_action).group(1)
    if who[0] == "--org":
        check(form_action.startswith("https://github.com/organizations/acme/settings/apps/new?state="),
              "form targets the org registration URL")
    else:
        check(form_action.startswith("https://github.com/settings/apps/new?state="),
              "form targets the personal registration URL")
    check(posted == dict(manifest, redirect_url=url + "callback"), "posted manifest equals input plus redirect_url")
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
            f.write(FAKE_SOPS)
        os.chmod(sops, stat.S_IRWXU)
        openssl = os.path.join(tmp, "openssl")
        with open(openssl, "w") as f:
            f.write(FAKE_OPENSSL.replace("@PEM@", repr(PEM)))
        os.chmod(openssl, stat.S_IRWXU)
        os.environ["FAKE_SOPS_LOG"] = os.path.join(tmp, "violations")

        def stored(path):
            return json.load(open(path))

        # existing file, all six keys under the prefix, other content kept
        sf = os.path.join(tmp, "new.json")
        rc, err, out = run(tmp, manifest, sf, api_port=port, preexisting='{"other": "keep"}')
        check(rc == 0, f"run exits 0 (got {rc}): {err}")
        d = stored(sf)
        want = {"app_id": "4242", "app_slug": "fleetkit-test", "app_pem": PEM, "client_id": "Iv1.cid",
                "client_secret": "CLSECRET", "webhook_secret": "WHSECRET"}
        for k, v in want.items():
            check(d.get(f'["github"]["app"]["{k}"]') == v, f"{k} stored under prefix")
        check(d.get("other") == "keep", "other content kept")
        for leak in ("TESTKEYMATERIAL", "WHSECRET", "CLSECRET"):
            check(leak not in out and leak not in err, f"{leak} not printed")
        for shown in ("fleetkit-test", "4242", "https://github.com/apps/fleetkit-test",
                      "https://github.com/apps/fleetkit-test/installations/new"):
            check(shown in out, f"{shown} printed")
        check(not os.path.exists(os.environ["FAKE_SOPS_LOG"]), "secrets never in sops argv or environment")

        # slashed prefix, --user
        sfu = os.path.join(tmp, "user.json")
        rc, err, _ = run(tmp, manifest, sfu, api_port=port, who=("--user", "acme"), prefix="a/b")
        check(rc == 0 and stored(sfu).get('["a"]["b"]["app_id"]') == "4242", f"--user with slashed prefix: {err}")

        # missing sops file is refused
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", os.path.join(tmp, "m.json"),
                            "--sops-file", os.path.join(tmp, "nope.yaml"), "--key-prefix", "x", "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "does not exist" in r.stderr, "missing sops file refused")

        # record installation
        env = dict(os.environ)
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--sops-file", sf, "--key-prefix", "github.app",
                            "--record-installation", "--sops", sops, "--openssl", openssl,
                            "--api", f"http://127.0.0.1:{port}"], capture_output=True, text=True, env=env)
        check(r.returncode == 0, f"record-installation exits 0: {r.stderr}")
        check(stored(sf).get('["github"]["app"]["installation_id"]') == "777", "installation_id of the org stored")
        check("TESTKEYMATERIAL" not in r.stdout + r.stderr, "record-installation prints no key")
        r = subprocess.run([sys.executable, TOOL, "--org", "nobody", "--sops-file", sf, "--key-prefix", "github.app",
                            "--record-installation", "--sops", sops, "--openssl", openssl,
                            "--api", f"http://127.0.0.1:{port}"], capture_output=True, text=True, env=env)
        check(r.returncode != 0 and "not installed" in r.stderr, "no installation for the org fails")

        # state mismatch is rejected but the real callback still works
        sf4 = os.path.join(tmp, "s.yaml")
        rc, err, _ = run(tmp, manifest, sf4, bad_state=True, api_port=port)
        check(rc == 0, f"state mismatch then good callback: {err}")

        # bad code: nonzero, nothing written
        sf5 = os.path.join(tmp, "b.yaml")
        rc, err, _ = run(tmp, manifest, sf5, code="expired", api_port=port)
        check(rc != 0 and open(sf5).read() == "{}", "bad code fails and writes nothing")

        # argument validation
        mf = os.path.join(tmp, "bad.json")
        json.dump({"name": "x", "url": "u", "default_permissions": {"contents": "owner"}}, open(mf, "w"))
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sf4, "--key-prefix", "x", "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "invalid level" in r.stderr, "bad permission level refused")
        json.dump({"name": "x", "url": "u", "default_permissions": {}, "hook_attributes": {"url": "u", "active": True}}, open(mf, "w"))
        r = subprocess.run([sys.executable, TOOL, "--org", "acme", "--manifest", mf, "--sops-file", sf5, "--key-prefix", "x", "--sops", sops],
                           capture_output=True, text=True)
        check(r.returncode != 0, "manifest with empty permissions refused")
        r = subprocess.run([sys.executable, TOOL, "--org", "-bad org", "--manifest", mf, "--sops-file", sf5, "--key-prefix", "x"],
                           capture_output=True, text=True)
        check(r.returncode != 0 and "invalid account name" in r.stderr, "bad org refused")
    srv.shutdown()
    status = "fail" if fails else "pass"
    print(json.dumps({"github_app_bootstrap": status}))
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
